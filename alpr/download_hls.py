"""Download a recorded YouTube live stream (one HLS video format, e.g. 270 = 1080p) as hourly MP4 pieces, many
segments in parallel (one connection is throttled to ~0.3 MB/s; 16 give ~3 MB/s).

    python -m alpr.download_hls uAVdyvglPco 270 data/videos/causeway/jb_ciq_cam02_2023-03-22
    python -m alpr.download_hls uMBo-qx9M2U 270 data/videos/causeway/jb_ciq_ipc_2023-03-21 --end-sec 28440

Piece k covers the HLS segments whose start time is in [k*piece_sec, (k+1)*piece_sec) (cut at segment
boundaries = keyframes) and is written as OUT_DIR/<id>_<fmt>_p<k>.mp4, with a .json sidecar holding its
start in stream seconds (sum of the playlist's EXTINF durations) - alpr.congestion and alpr.series use it
for the clock. Finished pieces are skipped, so the script can simply be run again after an interruption.
The playlist URL (it expires) is refreshed with yt-dlp, which must be installed (pip install yt-dlp).
"""
import argparse
import concurrent.futures as cf
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def fetch_meta(vid):
    out = subprocess.run([sys.executable, "-m", "yt_dlp", "-J", "--skip-download", f"https://www.youtube.com/watch?v={vid}"],
                         capture_output=True, text=True, encoding="utf-8", timeout=300)
    return json.loads(out.stdout)


class Playlist:
    def __init__(self, vid, fmt):
        self.vid, self.fmt = vid, fmt
        self.lock = threading.Lock()
        self.stamp = 0.0
        self.refresh()

    def refresh(self):
        with self.lock:
            if time.time() - self.stamp < 60 and self.stamp:
                return
            d = fetch_meta(self.vid)
            f = next(f for f in d["formats"] if f["format_id"] == self.fmt)
            self.headers = f.get("http_headers", {})
            req = urllib.request.Request(f["url"], headers=self.headers)
            text = urllib.request.urlopen(req, timeout=60).read().decode("utf-8")
            segs, dur = [], None
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("#EXTINF:"):
                    dur = float(line[8:].split(",")[0])
                elif line and not line.startswith("#"):
                    segs.append((urllib.parse.urljoin(f["url"], line), dur or 0.0))
                    dur = None
            self.segs = segs
            self.stamp = time.time()

    def get(self, i, tries=8):
        for k in range(tries):
            url = self.segs[i][0]
            try:
                req = urllib.request.Request(url, headers=self.headers)
                with urllib.request.urlopen(req, timeout=90) as r:
                    data = r.read()
                if not data or data[0] != 0x47:
                    raise IOError(f"segment {i}: not MPEG-TS")
                return data
            except urllib.error.HTTPError as e:
                if e.code in (403, 404, 410):
                    self.refresh()
                time.sleep(2 + 3 * k)
            except Exception:
                time.sleep(2 + 3 * k)
        raise IOError(f"segment {i} failed after {tries} tries")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("vid")
    ap.add_argument("fmt")
    ap.add_argument("out_dir")
    ap.add_argument("--end-sec", type=float, default=0.0)
    ap.add_argument("--piece-sec", type=float, default=3600.0)
    ap.add_argument("--conn", type=int, default=16)
    args = ap.parse_args()
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    os.makedirs(args.out_dir, exist_ok=True)
    pl = Playlist(args.vid, args.fmt)
    starts, t = [], 0.0
    for _, d in pl.segs:
        starts.append(t)
        t += d
    n = len(pl.segs)
    if args.end_sec > 0:
        n = sum(1 for s in starts if s < args.end_sec)
    pieces = {}
    for i in range(n):
        pieces.setdefault(int(starts[i] // args.piece_sec), []).append(i)
    print(f"{args.vid} fmt {args.fmt}: {n} segments ({starts[n - 1] / 3600 + pl.segs[n - 1][1] / 3600:.2f} h) "
          f"in {len(pieces)} pieces -> {args.out_dir}", flush=True)
    t0, total = time.time(), 0
    for k in sorted(pieces):
        idx = pieces[k]
        base = os.path.join(args.out_dir, f"{args.vid}_{args.fmt}_p{k:02d}")
        if os.path.exists(base + ".json") and os.path.exists(base + ".mp4"):
            continue
        parts = base + ".parts"
        os.makedirs(parts, exist_ok=True)
        lock, state = threading.Lock(), {"n": 0, "bytes": 0, "last": time.time()}

        def work(i):
            p = os.path.join(parts, f"{i:06d}.ts")
            if os.path.exists(p) and os.path.getsize(p) > 0:
                return
            data = pl.get(i)
            with open(p + ".tmp", "wb") as f:
                f.write(data)
            os.replace(p + ".tmp", p)
            with lock:
                state["n"] += 1
                state["bytes"] += len(data)
                if time.time() - state["last"] > 60:
                    state["last"] = time.time()
                    el = time.time() - t0
                    print(f"  piece {k:02d}: {state['n']}/{len(idx)} segments, "
                          f"{(total + state['bytes']) / max(1e-6, el) / 1e6:.2f} MB/s overall", flush=True)

        with cf.ThreadPoolExecutor(args.conn) as ex:
            list(ex.map(work, idx))
        total += state["bytes"]
        # concatenate the piece's segments in order through ffmpeg (no intermediate .ts copy)
        proc = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "mpegts", "-i", "pipe:0",
                                 "-map", "0:v:0", "-c", "copy", "-avoid_negative_ts", "make_zero", base + ".mp4"],
                                stdin=subprocess.PIPE)
        for i in idx:
            with open(os.path.join(parts, f"{i:06d}.ts"), "rb") as f:
                shutil.copyfileobj(f, proc.stdin)
        proc.stdin.close()
        if proc.wait() != 0:
            raise SystemExit(f"ffmpeg failed on piece {k}")
        side = {"video_id": args.vid, "format": args.fmt, "piece": k, "start_sec": round(starts[idx[0]], 3),
                "duration_sec": round(sum(pl.segs[i][1] for i in idx), 3), "segments": [idx[0], idx[-1] + 1]}
        with open(base + ".json", "w", encoding="utf-8") as f:
            json.dump(side, f, indent=1)
        shutil.rmtree(parts)
        el = time.time() - t0
        print(f"piece {k:02d} done: {os.path.getsize(base + '.mp4') / 1e9:.2f} GB, start {side['start_sec']:.1f} s, "
              f"{side['duration_sec']:.1f} s | {total / 1e9:.2f} GB in {el / 60:.1f} min ({total / el / 1e6:.2f} MB/s)",
              flush=True)
    print("all pieces done", flush=True)


if __name__ == "__main__":
    main()
