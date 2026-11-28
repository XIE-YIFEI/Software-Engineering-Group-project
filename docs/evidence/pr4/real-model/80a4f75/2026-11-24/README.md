# PR #4 real-model acceptance record

Author: Yifei Xie

Validation date: 24 November 2026 (Asia/Kuala_Lumpur)

Candidate: `80a4f758ad7cb277ce933005f90d356b1e2194f4`

Corresponding recorded PR head: `12706496a88bcd33889075500d87ef9ac15125e5`

Toby asynchronous review: **PENDING REVIEW**

This records the retained real-model/browser observations at the fixed candidate. Local PASS does not mean Toby has reviewed or approved the evidence, full PR acceptance, or permission to merge. The recorded head identifies the execution context; this document makes no fresh remote head, CI or review-state assertion and does not claim coverage of later revisions.

| Scenario | Job / run | Local result | Observed final state |
| --- | --- | --- | --- |
| A: normal completion and continuous playback | 1 / 1 | PASS, with separate playback supplement | `done`, complete, 1,020 frames, four records; same-run browser playback reached natural end. |
| B: browser Cancel after own saved checkpoint | 2 / 2 | PASS within this scenario | `cancelled`, incomplete, 432 saved frames, 0-14.4 s, one recovered record. Recognition and concurrent annotated encoder exited 0; playback conversion did not start. |
| C: controlled recognition-process loss | 3 / 3 | PASS for partial-result recovery | `failed`, incomplete, 249 saved frames, 0-8.3 s, one recovered record. Only the verified recognition process was terminated; observed exit code 15. |
| D1: first conversion-cancel attempt | 4 / 4 | ATTEMPTED; target NOT COVERED | Recognition exited 1 after checkpoint replacement raised WinError 5. Partial recovery retained 249 frames and one record. Playback conversion never started; no Cancel action. |
| D2: second conversion-cancel attempt | 5 / 5 | ATTEMPTED; target NOT COVERED | Real playback conversion exited 0 and the job completed normally with four records. No Cancel action occurred; cancellation and cancellation cleanup were not tested. |

D remains incomplete. D3 is preparation only and was not started. All scenarios remain **PENDING REVIEW** by Toby.

## Execution and attribution

The approved prepared development clip represents original interval [48,82), processed once as clip-local [0,34): 34 seconds, 1,020 frames at 30 fps. The configuration used the LILIN spatial scene, camera 1 and OpenVINO CPU. Recorded at was the notional `2026-10-14 08:00:48` MYT, separate from the actual validation date. Annotated output was enabled for A/B and disabled for C/D1/D2. A/B/C used the prepared H.264 MP4; D1/D2 used the already prepared same-window MPEG-4 AVI to exercise natural playback conversion. No input extension, encoding slowdown or production change was used.

Browser submission and B cancellation were performed by the user. C used the previously authorized, identity-checked single-process fault injection. The retained reports attribute individual actions and process observations. Candidate attribution comes from execution/worktree records; the production result JSON does not itself embed a Git SHA. No Wasi held-out footage, labels or adjudication was used for this acceptance.

## Review materials and delivery identity

Original artifacts are **not included in this public directory**. No ZIP, footage, screenshots, PDFs, checkpoint content, database, credentials or raw logs are copied here. The manifest is metadata identifying selected retained artifacts; its paths are not repository links or public downloads.

Supporting artifacts are **available separately for team review**. A real team-accessible sharing link has not been supplied; delivery and review are pending. Nothing was sent or uploaded as part of preparing this record.

| Retained ZIP | Value |
| --- | --- |
| Filename | `PR4_Toby_20261124T145834035682Z.zip` |
| Size | 118,004,777 bytes |
| SHA-256 | `ddb5e6ff5b78249a490f1f94e79b452405e83af78f1713f2b3660ad483e776c6` |
| Contents | 483 files in the named top-level directory; six original formal recordings, including A's separate full-playback supplement, reports, logs and supporting evidence. |
| ZIP verification | Entry list and CRC checked; all extracted file sizes and SHA-256 values matched the source directory. |

The unarchived review-package root is `PR4_Toby_20261124T145834035682Z/`. In [evidence_manifest.csv](evidence_manifest.csv), `relative_path` is relative to that root when `artifact_scope=review_package`, or to the ZIP's containing handoff directory when `artifact_scope=handoff_directory`. Every row has `included_in_public_version=false`; retained artifact bytes are not reproduced here. The manifest is a selected review index, not the complete package inventory. The separately retained `package_files.json` and `SHA256SUMS.txt` cover the package according to their documented exclusions.

Use [review_navigation.md](review_navigation.md) for checked recording offsets and [limitations.md](limitations.md) for coverage boundaries. A's first recording misses approximately 0.7 seconds of the playback tail; its distinct same-run supplement supplies full continuous-playback evidence without splicing or another model run. D2's approximately 2.66-second interval is from process creation to the first observation of exit, not precise conversion duration.

This new dated record does not edit or replace the [19 November synthetic-fixture conclusions](../../../targeted/80a4f75/2026-11-19/README.md). Historical statements in those documents remain statements about their original validation date. This update prepares text only; it adds no new model execution or review approval.

## Later supplement (28 November 2026)

The record above remains the historical 24 November result, including D1/D2 target NOT COVERED and D3 not yet started at that date. The [D3 Rev2 supplement](../2026-11-28/README.md) adds job 6/run 6 local PASS and correctly separates exit-code observation from first observed process-handle signalling. A/B/C/D are local PASS. According to the user-provided message, Toby reviewed earlier A/B/C and D3, explicitly confirmed D3 PASS on 80a4f75, and intends to record reviewed A–D acceptance notes. No message timestamp was supplied; GitHub notes and merge were not verified or claimed. D1/D2 history and the original delivery identity remain unchanged, and D1's WinError 5 cause remains unconfirmed.
