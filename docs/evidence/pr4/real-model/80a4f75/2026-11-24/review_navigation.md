# Navigation for separately retained recordings

Author: Yifei Xie. Toby: **PENDING REVIEW**. No media is embedded or included here. Artifacts are **available separately for team review**; sharing link pending.

Offsets below are actual decoded **recording PTS in seconds from the start of each separate original**, extracted from the already checked `recording_navigation.json`. They are not clip elapsed time, wall clock or exact mouse-down timestamps. The private navigation retains the associated original-frame checks and report/log paths; [evidence_manifest.csv](evidence_manifest.csv) identifies the retained bytes. Folder names are preserved to distinguish the four files named `original_live_capture.mkv`.

| Scene / job / run | Recording filename and distinguishing parent | Checked recording PTS (s) | What to inspect |
| --- | --- | --- | --- |
| A / 1 / 1 | `A_normal_20261124T035308652589Z/A_original_screen_recording.mkv` | 961.933 | Prepared MP4, LILIN configuration and Recorded at; annotated output ON. |
| A / 1 / 1 | Same original | 1054.333 | New job 1 queued after submission. |
| A / 1 / 1 | Same original | 1712.133 | Job 1 done, 100%, four records. |
| A / 1 / 1 | Same original | 2285.533 | Last frame, clip HUD 33.3 s; this original lacks approximately 0.7 s of the playback tail. |
| A supplement / 1 / 1 | `playback_supplement_20261124T045225010305Z/A_same_run_playback_original.mkv` | 0.000 | Same run paused at video 0:00 / 0:34. |
| A supplement / 1 / 1 | Same supplement | 24.000 | Moving continuous playback, clip HUD 14.1 s. |
| A supplement / 1 / 1 | Same supplement | 43.000 | Still playing, HUD 33.1 s; not natural end. |
| A supplement / 1 / 1 | Same supplement | 44.000 | Natural end, HUD 34.0 s, player 0:34 / 0:34 and Play icon. Review the unedited start-to-end sequence for continuity. |
| B / 2 / 2 | `recording_formal_20261124T061837079958Z/original_live_capture.mkv` | 695.733 | Cancellation requested / saving results notice; not an exact click timestamp. |
| B / 2 / 2 | Same original | 699.133 | Cancelled/incomplete, 432 saved frames, 0-14.4 s. |
| C / 3 / 3 | `recording_formal_20261124T064713953307Z/original_live_capture.mkv` | 3242.867 | Job 3 still running before controlled recognition loss. |
| C / 3 / 3 | Same original | 3244.933 | Failed/incomplete, 249 saved frames, 0-8.3 s. The fault CLI itself is recorded in separate injector JSON, not shown in this Chrome-only capture. |
| D1 / 4 / 4 | `recording_formal_20261124T100345941343Z/original_live_capture.mkv` | 793.467 | Uploading approved prepared AVI; annotated output OFF. |
| D1 / 4 / 4 | Same original | 925.000 | Failed/incomplete with WinError 5 checkpoint-replacement traceback. No playback conversion or Cancel action occurred. |
| D2 / 5 / 5 | `recording_formal_20261124T135357141765Z/original_live_capture.mkv` | 854.867 | Job 5 done, 100%, four records; no Cancel performed. |
| D2 / 5 / 5 | Same original | 958.200 | Run 5 result list with four readable plate crops. |

For each scenario, start with its formal `scenario_*_report*.txt`, then the result JSON, own retained checkpoint/final report, recognition log, process timeline, terminal metadata and recording validation. These files are referenced by relative path and hash in the metadata manifest; none of their raw contents are published here. A's two recordings are separate originals with different purposes, not a spliced composite or a second model run.

D2 conversion has process-clock evidence only: creation 22:07:32.072526 MYT; exit 0 first observed 22:07:34.731999 MYT. Approximately 2.66 s is creation to first observed exit, **not precise conversion duration**. No recording PTS is assigned to conversion start, exit or a nonexistent Cancel action. See [limitations.md](limitations.md) for the timing and UI-state uncertainty. D1/D2 do not pass D.
