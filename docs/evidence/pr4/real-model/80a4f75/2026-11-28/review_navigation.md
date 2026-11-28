# D3 final edited-video navigation — Rev2

Job 6/run 6; local PASS. Toby review confirmed according to the user-provided message; no explicit review-message timestamp supplied. GitHub acceptance notes and merge were not independently checked.

Final file in the separate package: `evidence/recording/D3_website_recording.mp4`, an **edited presentation video**, 4,986,745 bytes, SHA-256 `ce1d70f92b02546a16a5cd73b760ca20527c39e2015eb56d28284f65b4f2624d`, duration 10:12.800. No original recording link is provided; the original was deleted at user request and was not restored. Previous source-PTS navigation is historical only.

The table uses actual edited-file PTS previously checked from decoded frames; no video or application retest was performed for Rev2. On-video source-segment footer times are not edited seek positions.

| Edited seek | Edited PTS (s) | Checked observation |
| --- | --- | --- |
| 00:00.000 | 0.000 | Configuration before submission: prepared AVI selected, LILIN camera/scene, render OFF and notional date/time. |
| 00:04.000 | 4.000 | The button displays Uploading; no second submission is shown. |
| 00:06.000 | 6.000 | New job 6 is running/starting; distinguish it from the old job 5. |
| 09:15.000 | 555.000 | Job 6 still shows running at 98%, with its own Cancel visible. |
| 09:21.000 | 561.000 | Cursor is on job 6 Cancel. This is not a separately instrumented mouse-down timestamp. |
| 09:22.200 | 562.200 | Job 6 displays cancelled / incomplete: 1,020 saved frames, video 0–34 s, four plate records; traffic totals unavailable. |
| 09:24.000 | 564.000 | Run 6 incomplete result list is visible. The cropped viewport shows some entries; the total of four is corroborated by the terminal/log evidence. |

Pointer position is not an exact mouse-down timestamp. The video crop excludes the off-website prompt/clock strip and has no audio track; those event times are in retained logs. In particular, 01:34:51.953 MYT is **exit code first observed**, whereas 01:34:52.102 MYT is **process handle first observed signalled**. Neither is a precise exit time. See [timing_errata_rev2.md](timing_errata_rev2.md) and [limitations.md](limitations.md).
