# Limitations and outstanding review

Author: Yifei Xie. Validation date: 24 November 2026 (Asia/Kuala_Lumpur). Candidate: `80a4f758ad7cb277ce933005f90d356b1e2194f4`. Toby: **PENDING REVIEW**.

## A: two recordings with different purposes

The first original `A_original_screen_recording.mkv` contains configuration, submission, completion, result/evidence display and most playback. Its last decoded recording PTS is 2285.533 s, with video HUD 33.3 s: approximately 0.7 s of the 34-second playback tail is missing. Recorder shutdown after `q` exceeded the wait limit; only the verified recorder was terminated, exit 1. Original bytes were preserved, and full decoding succeeded. This first recording alone fails full-tail coverage.

`A_same_run_playback_original.mkv` is a separate unedited capture of the same run. It shows paused 0:00, continuous moving playback and natural 0:34/0:34 end at recording PTS 44.000 s. At PTS 43.000 s it is still playing. The supplement recorder stopped normally, exit 0, and full decoding succeeded. No splicing or second inference was performed. Local A PASS relies on the distinct execution recording and playback supplement together.

## D1: immediate failure known, historical cause unconfirmed

The failing operation was `os.replace(temp_path, path)` in the atomic JSON writer, replacing the job's temporary checkpoint with its existing `result.partial.json`; Windows returned WinError 5. The retained checkpoint contains 249 frames and one record. The last checkpoint was observed at 10:19:03.093890 UTC; recognition exit 1 was first observed at 10:19:09.828817 UTC. Exact syscall time was not recorded. Playback conversion never started.

The former outer watcher and child observer polled active checkpoint content at approximately 0.05 s and 0.1 s intervals. Source inspection found context-managed closure rather than a persistent read handle. Separate historical mock-file experiments showed that ordinary read handles and concurrent polling can interfere with replacement, but did not establish the lock owner in D1 or an application failure rate. Post-event attributes, ACLs and handle checks cannot exclude a transient earlier accessor. AVI input is not an established cause.

The separately saved D2 observer revision deferred checkpoint content reads, copies and hashes until terminal state and no verified service children. Active observation used existing logs, process metadata and file existence/size, with read-only task status. D2's success does **not** identify D1's root cause, prove that all interference was eliminated or establish a checkpoint-concurrency fix. Production code was not changed.

## D2: observation interval and missed cancellation

Application playback FFmpeg creation was recorded at 22:07:32.072526 MYT; its exit 0 was first observed at 22:07:34.731999 MYT on 24 November. The difference, 2.659473 s (approximately 2.66 s), is the **interval from creation to the first observation of exit**, not exact conversion duration. Actual exit occurred no later than that observation. The first live gate and identity recheck occurred about 144.576 ms and 237.229 ms after creation.

Browser-control transport was unavailable, so the workflow used a chat prompt and manual user operation. The user reported the task was already done; no Cancel was clicked or accepted. Exact prompt delivery, human reaction and browser action latency were not measured, so the missed window cannot be attributed solely to human reaction. There was no automatic click attempt to time.

The sparse/VFR recording has no synchronized wall clock or conversion-gate UI snapshot. Cancel visibility and enabled state during the exact conversion interval were not directly established. Source logic and nearby recorded queued states do not prove that exact UI state. Recording PTS must not be mapped to conversion wall-clock instants by guesswork, and an acceptance timestamp earlier than the first observed exit alone would not prove acceptance before actual exit.

D2's normal process exit and lack of task survivors establish normal-completion observations only. They do not establish conversion cancellation or cancellation cleanup. Recorder/decode FFmpeg exit codes are separate from application conversion evidence.

## Outstanding scope

- D is ATTEMPTED, target conversion-cancel stage NOT COVERED, and NOT PASS. D3 preparation exists separately; D3 was not started. Its future detection/click budgets are planning targets, not measured guarantees; recorded readiness was NO-GO because browser controls were unavailable.
- B covers cooperative browser cancellation after its own checkpoint; its application playback conversion did not start. C covers one controlled recognition-process failure and partial recovery, not automatic restart or every natural crash/strong-kill boundary.
- Full role/site/browser/codec coverage, partial annotated-video playback, extreme timeout/kill escalation, abrupt server-death/orphan recovery and other outstanding manual checklist items are not completed by these scenarios. No OCR accuracy or universal playback/process-cleanup guarantee is claimed.
- The source input copies are not duplicated in the review package. B/C source-video previews therefore require the retained input for full offline replay; their recordings, checkpoint and crop/scene evidence remain available separately. D1's AVI source fallback was not playable in Chrome; this is separate from D cancellation.
- Toby's asynchronous review remains PENDING REVIEW. A local PASS is not a team approval, merge authorization or confirmation of delivery. The separately available package has no supplied team-sharing link.
- Other owners' held-out hash, annotation and traffic/STOPPED tasks are outside this record and are not assigned to Yifei by this update. No new trajectory-export or scoring contract is implied.

The public directory contains only authored text and an artifact metadata manifest. Original video pixels, logs and other artifacts remain separate; hashes identify retained bytes and do not establish public redistribution rights or close the acceptance matrix. Earlier synthetic-fixture conclusions are preserved unchanged.
