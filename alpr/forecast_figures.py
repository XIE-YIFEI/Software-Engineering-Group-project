"""Figures of the congestion study for the docs (static PNG, light theme):

    python -m alpr.forecast_figures --out docs/images

  causeway_validation.png  measured vehicles on each approach (1-minute series) with the hand counts
  forecast_to_ciq.png      each test day: measured fill and the 30-minute forecasts of three models

Palette: the first four slots of the validated default categorical palette (dataviz skill,
references/palette.md: blue, orange, aqua, yellow, adjacent-pair CVD-safe); the congestion threshold uses
the reserved status colour "critical" with its own label. Lines 2 px, hairline grid, one y-axis per panel.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime

import numpy as np

SURFACE, INK, INK2, MUTED, GRID, BASE = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]          # categorical slots 1-4, fixed order
CRITICAL = "#d03b3b"


def _style(ax, title: str, ylabel: str) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(BASE)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(axis="y", color=GRID, linewidth=1)
    ax.set_axisbelow(True)
    ax.set_title(title, loc="left", fontsize=11, color=INK, pad=10)
    ax.set_ylabel(ylabel, fontsize=9, color=INK2)


def _hours(times):
    return np.array([(t.hour + t.minute / 60 + t.second / 3600) for t in times])


def _clock_axis(ax, lo: float, hi: float) -> None:
    ticks = np.arange(np.ceil(lo), np.floor(hi) + 1)
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{int(t):02d}:00" for t in ticks])
    ax.set_xlim(lo, hi)


def validation_figure(cams, out: str) -> None:
    """cams: [(title, series_csv, counts_txt, calibration_json, entity)]."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from .congestion import load_counts
    from .forecast import load_series
    fig, axes = plt.subplots(len(cams), 1, figsize=(11, 3.3 * len(cams)), facecolor=SURFACE)
    axes = np.atleast_1d(axes)
    for ax, (title, csv_path, counts, cal_path, ent) in zip(axes, cams):
        d = load_series(csv_path)
        h = d.minute_of_day / 60.0
        y = d.get(ent, "density")
        ax.plot(h, y, color=SERIES[0], lw=2, solid_joinstyle="round", label="measured (1-minute mean)")
        tot = {}
        for t, z, n in load_counts(counts):
            tot[t] = tot.get(t, 0) + n
        th = [int(t[:2]) + int(t[3:5]) / 60 + int(t[6:8]) / 3600 for t in tot]
        ax.scatter(th, list(tot.values()), s=42, color=SERIES[1], edgecolors=SURFACE, linewidths=2, zorder=3,
                   label="hand count (one keyframe)")
        with open(cal_path, encoding="utf-8") as f:
            cal = json.load(f)
        g = cal["groups"][ent]
        cap = sum(cal["capacity"].values())
        ax.axhline(cap / 2, color=CRITICAL, lw=1.5, ls=(0, (4, 3)))
        ax.text(h.min() + 0.05, cap / 2 + 1, f"congestion: half the jam capacity ({cap / 2:.0f} vehicles)",
                color=INK2, fontsize=8, va="bottom")
        _style(ax, f"{title}  ·  leave-one-out error {g['loo_mae']:.1f} vehicles on a mean of "
                   f"{g['mean_count']:.0f} ({100 * g['relative_mae']:.0f} %)", "vehicles on the approach")
        _clock_axis(ax, np.floor(h.min()), np.ceil(h.max()))
        ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK2)
    fig.tight_layout()
    fig.savefig(out, dpi=110, facecolor=SURFACE)
    plt.close(fig)


def forecast_figure(results_json: str, out: str, models=("LightGBM", "GRU", "daily profile"), h: int = 30,
                    window=(5.5, 10.0)) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from .forecast import congestion_state, onsets
    with open(results_json, encoding="utf-8") as f:
        res = json.load(f)
    thr = res["congestion_threshold"]
    pairs = [p for p in res["pairs"]]
    fig, axes = plt.subplots(len(pairs), 1, figsize=(11, 3.4 * len(pairs)), facecolor=SURFACE)
    axes = np.atleast_1d(axes)
    for ax, p in zip(axes, pairs):
        times = [datetime.fromisoformat(t) for t in p["times"]]
        hrs = _hours(times)
        y = np.array([np.nan if v is None else v for v in p["measured"]])
        sel = (hrs >= window[0]) & (hrs <= window[1])
        ax.plot(hrs[sel], y[sel], color=SERIES[0], lw=2, label="measured")
        for k, name in enumerate(models):
            fc = np.array([np.nan if v is None else v for v in p["predictions"][name][str(h)]])
            # forecast made at t for t+h, drawn at t+h
            xs, ys = hrs[h:], fc[:-h]
            s2 = (xs >= window[0]) & (xs <= window[1])
            ax.plot(xs[s2], ys[s2], color=SERIES[k + 1], lw=2, label=f"{name}, made {h} min before")
        st = congestion_state(y, thr)
        for T in onsets(st):
            if window[0] <= hrs[T] <= window[1]:
                ax.axvline(hrs[T], color=MUTED, lw=1)
                ax.text(hrs[T] + 0.03, 0.02, f"onset {times[T]:%H:%M}", color=INK2, fontsize=8,
                        transform=ax.get_xaxis_transform())
        ax.axhline(thr, color=CRITICAL, lw=1.5, ls=(0, (4, 3)))
        ax.text(window[0] + 0.03, thr + 0.02, "congestion (critical density, fill 0.5)", color=INK2, fontsize=8)
        names = {"jb_ciq_cam02_2023-03-22": "A · Camera 02 · Wed 22 Mar 2023",
                 "jb_ciq_ipc_2023-03-21": "C · IPC camera · Tue 21 Mar 2023"}
        tr = p["train"].split("@")[0]
        te = p["test"].split("@")[0]
        _style(ax, f"Test day {names.get(te, te)}  ·  trained on {names.get(tr, tr).split(' · ')[0]} only",
               "fill (share of jam capacity)")
        _clock_axis(ax, *window)
        ax.set_ylim(0, max(1.0, np.nanmax(y[sel]) * 1.1))
        ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=INK2)
    fig.tight_layout()
    fig.savefig(out, dpi=110, facecolor=SURFACE)
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="docs/images")
    ap.add_argument("--results", default="outputs/forecast/to_ciq_AC_fill_pool.json")
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    cams = [("C · IPC camera · Tue 21 Mar 2023 · to CIQ", "data/series/jb_ciq_ipc_2023-03-21.csv",
             "alpr/gt/causeway/jb_ciq_ipc_2023-03-21.txt", "outputs/causeway/jb_ciq_ipc_2023-03-21.calibration.json",
             "To CIQ"),
            ("A · Camera 02 · Wed 22 Mar 2023 · to CIQ", "data/series/jb_ciq_cam02_2023-03-22.csv",
             "alpr/gt/causeway/jb_ciq_cam02_2023-03-22.txt",
             "outputs/causeway/jb_ciq_cam02_2023-03-22.calibration.json", "To CIQ")]
    validation_figure(cams, os.path.join(args.out, "causeway_validation.png"))
    forecast_figure(args.results, os.path.join(args.out, "forecast_to_ciq.png"))
    print(f"figures -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
