# D3 timing errata and reviewer update — Rev2

| Correct label | MYT (28 November 2026) | Raw precision and source |
| --- | --- | --- |
| **exit code first observed** | **01:34:51.953** | 01:34:51.953209; code 1; process-observer line 48 |
| **process handle first observed signalled** | **01:34:52.102** | 01:34:52.102313; `WAIT_OBJECT_0`; raw prompt-log line 197 |

Neither is a precise process-exit time. Line 196 still records the same retained handle as `WAIT_TIMEOUT` at 01:34:52.080732 MYT. These observations concern different API results. Cancellation audit at 01:34:51.846 and DB-already-accepted then same-handle-live samples from 01:34:51.859378 preserve the local PASS conclusion.

## Reviewer source

The user provided this report of Toby's message: Toby reviewed earlier A/B/C evidence and D3, explicitly confirmed “D3 passes on 80a4f75”, and said he will record reviewed A–D results in PR #4 acceptance notes. Current status is **Toby review confirmed (user-provided message)**. No explicit message timestamp was supplied; review time is unknown and not inferred from document preparation. No independent retrieval of the original message, GitHub notes or merge status was performed. Toby's intention is not recorded as an already completed GitHub update.

## Revision relationship

Archived reports and historical ZIPs are unchanged. The new separate Rev2 delivery contains `ERRATA_REV2_EN.txt`, `report_revision_relation.json`, `reports/scenario_D3_report_zh_rev2.txt`, `reports/scenario_D3_result_rev2.json`, `D3_summary_en_rev2.txt` and raw unchanged timing sources. Earlier pending-review labels and ambiguous first-exit wording remain historical version statements; this revision supersedes them for current documentation.

D1/D2 history, the unconfirmed WinError 5 cause, three malformed late prompt-log lines, AVI playback limitation and edited-video/deleted-original disclosure remain in force. No new test, commit, push, message or PR action was performed. See [README.md](README.md) for the new ZIP identity and [reviewer_confirmation.json](reviewer_confirmation.json) for explicit source/timestamp/claim boundaries.
