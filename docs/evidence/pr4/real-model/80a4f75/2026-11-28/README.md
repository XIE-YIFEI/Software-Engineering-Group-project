# PR #4 D3 acceptance supplement — Rev2

Fixed execution candidate: `80a4f758ad7cb277ce933005f90d356b1e2194f4`. Original test date: 28 November 2026 MYT. This is a documentation correction and reviewer-status update, not a new test.

## Current result and reviewer confirmation

A/B/C/D remain **local PASS**. **Toby review confirmed, based on the user-provided message.** The user reports that Toby reviewed earlier A/B/C evidence and the D3 supplementary package, explicitly confirmed “D3 passes on 80a4f75”, and said he will record reviewed A–D results in PR #4 acceptance notes.

No explicit timestamp was supplied for that message. The review time is unknown; document-preparation time is not presented as review time. Toby's stated future action is not a claim that GitHub acceptance notes already exist. No independent original-message/GitHub acceptance-note check or merge verification was performed. This records the user's supplied confirmation, not a new PR operation. See [reviewer_confirmation.json](reviewer_confirmation.json).

| Scenario | Job / run | Local result | User-reported review scope |
| --- | --- | --- | --- |
| A | 1 / 1 | PASS using its original execution capture and separate same-run full-playback supplement | Earlier evidence reviewed by Toby |
| B | 2 / 2 | PASS for browser cancellation after its own checkpoint | Earlier evidence reviewed by Toby |
| C | 3 / 3 | PASS for the controlled recognition-process-loss recovery scenario | Earlier evidence reviewed by Toby |
| D, via D3 | 6 / 6 | PASS for actual playback conversion cancellation and incomplete recovery/cleanup | D3 explicitly confirmed PASS on 80a4f75 |

The [24 November record](../2026-11-24/README.md) remains historical: D1/job 4 failed before playback conversion with WinError 5; D2/job 5 completed normally without Cancel. Those attempts remain target NOT COVERED. D3 adds coverage without retroactively passing D1/D2 or establishing a fix for D1's unconfirmed cause.

## Corrected process observation labels

All times below are on 28 November 2026 MYT (UTC+08:00). The raw UTC observations fall on 27 November.

| Observation | MYT | Evidence |
| --- | --- | --- |
| Cancellation accepted in application audit | 01:34:51.846 | One `job_cancel` entry and browser request HTTP 200 |
| DB already showed accepted cancellation; same retained process handle then returned `WAIT_TIMEOUT` | 01:34:51.859378 | First of 11 contiguous valid ordered samples, prompt-log lines 186–196 |
| **exit code first observed** | **01:34:51.953** | Code 1; raw precision 01:34:51.953209; process-observer line 48 |
| Same retained process handle still returned `WAIT_TIMEOUT` | 01:34:52.080732 | Prompt-log line 196 |
| **process handle first observed signalled** | **01:34:52.102** | `WAIT_OBJECT_0`; raw precision 01:34:52.102313; prompt-log line 197 |

**Neither 01:34:51.953 nor 01:34:52.102 is a precise process-exit time.** They are observations of different API results and must not be conflated. The acceptance-while-same-handle-live evidence remains valid, so local PASS is unchanged. See [timing_errata_rev2.md](timing_errata_rev2.md). The original logs and archived reports are not rewritten.

Actual application playback FFmpeg PID 41428 had birth epoch `1795800884.5453265`, the verified bundled executable, this task's uploaded AVI input and `job_6/playback.mp4` output. Its ancestry reached server 32500 through wrappers 40764 and 19312. Recording FFmpeg PID 15180 was separate. Recognition leaf PID 45436's exit code 0 was first observed at 01:34:42.836703 MYT; the live conversion gate was 01:34:45.264464 with 48 positive output bytes. Prompt visibility/sound-call onset were 01:34:48.366665 / 01:34:48.375821 MYT. Exact mouse-down was not separately timestamped.

## Configuration, retained result and limitations

The same prepared 34-second MPEG-4 AVI was processed as clip-local [0,34), 1,020 frames at 30 fps, original development interval [48,82). LILIN scene, camera 1, OpenVINO CPU/headless, annotated output OFF, start=0/duration=0 and notional Recorded at `2026-10-14 08:00:48` MYT were retained. No second trim, intentional slowdown or production change was used. The user manually submitted and cancelled; no agent browser control or API substitute was used for that executed session.

Job 6/run 6 is `cancelled`/incomplete, with 1,020 saved frames, [0,34] seconds and four records. Own `result.partial.json` is selected (SHA-256 `5f5ecac85a2d5f1284469ecd632a86e49d99bee75d42b415acfb8bed20afc60c`); recognition final JSON exists but is not selected. The source AVI is attributed as fallback; partial playback MP4 is not referenced. **Source AVI fallback does not prove successful browser playback.** Task/helper cleanup observations and imported crop/scene readability are unchanged.

The three malformed late prompt-log lines 1584/1856/2055 remain preserved. Critical timing records, including the line 197 signalled observation, precede them. Later prompt-sampling completeness and the malformed-line cause are not claimed. **D1 WinError 5 root cause remains unconfirmed; D3 success does not prove it fixed.** Full scope limitations remain in [limitations.md](limitations.md).

## Confirmed final video and corrected delivery

`D3_website_recording.mp4` remains the final **edited presentation video**, byte-identical to the user-confirmed file: 4,986,745 bytes; SHA-256 `ce1d70f92b02546a16a5cd73b760ca20527c39e2015eb56d28284f65b4f2624d`; duration 10:12.800. VS Code/chat removal was the privacy purpose; actual processing also included website cropping, short/transition interval omissions, footer labeling and re-encoding. It is never called an unedited original.

The original recording was deleted earlier at the user's request; it is not restored and no original link is provided. Historical original-video hashes and prior checks are retained as historical metadata only. Current edited navigation: 09:21.000 pointer on Cancel, 09:22.200 cancelled/incomplete, 09:24.000 result list. The final crop excludes the prompt strip/original clock overlay and the video has no audio track; retained logs supply those event times. See [review_navigation.md](review_navigation.md).

| New Rev2 delivery identity | Value |
| --- | --- |
| ZIP | `PR4_D3_REVIEWED_REV2_20261128T1326352919261Z.zip` |
| Size | 14,835,940 bytes |
| SHA-256 | `c9fc7e5b757a13fc7f2d7e4c04c938175132c3570830d067c81be4b83001be7c` |
| Files | 100 under `PR4_D3_REVIEWED_REV2_20261128T1326352919261Z/` |
| Integrity | ZIP CRC, exact entry list and all extracted sizes/SHA-256 verified |

The prior final ZIP and all historical delivery ZIPs remain byte-identical. Rev2 provides separate report/result revision files and errata with version relationships. Raw logs and the confirmed video are unchanged. Only authored text and artifact metadata are prepared in this worktree; no raw video/log/checkpoint/database/credential bytes are added to Git. Package paths in [evidence_manifest.json](evidence_manifest.json) refer to separately retained material, not public download links.

This full uncommitted diff includes the earlier D3 result plus this correction against evidence-branch base `7d5dd8092f6e053f2bb4de22c6098a6e614d9b33`. No test, commit, push, outbound message, PR operation, production-code change or database modification occurred during this revision.
