# PR #4 scoped acceptance summary

Author: Yifei Xie  
Validation date: 19 November 2026 (Asia/Kuala_Lumpur)  
Text-only version prepared: 20 November 2026 (Asia/Kuala_Lumpur)

Repository: XIE-YIFEI/Software-Engineering-Group-project  
PR head: `12706496a88bcd33889075500d87ef9ac15125e5`  
Evidence candidate: `80a4f758ad7cb277ce933005f90d356b1e2194f4`

This summary records scoped PASS for evidence display, captions and PDF export checks for four explicitly synthetic fixtures. It does not establish full PR acceptance or approval to merge.

| Scenario | Recorded result within the accepted scope |
| --- | --- |
| source_missing | PASS: uploaded-video fallback title; stored-scene-unavailable explanation; source video at 2.4 s; synthetic and incomplete labels retained. |
| source_corrupt | PASS: unreadable stored scene falls back to the uploaded source at 2.4 s, with the correct title/caption and synthetic/incomplete labels. |
| annotated_missing | PASS: annotated-video title and video-with-system-overlays caption at 2.4 s; synthetic labels retained. The fixture has complete=true, so no incomplete marker is expected. |
| scene_control | PASS: valid stored scene takes precedence over fallback; Scene frame title; frame 227 at 7.57 s; green/yellow system-drawn box explanation; synthetic/incomplete labels retained. |

The source report attributes manual HTML/PDF viewing and export confirmation to the user. Eleven archived browser screenshots corroborate visible page portions. A separate review parsed all four existing PDFs and visually inspected all eight pages, including complete embedded images, candidate identity and fixture/image hashes. No evidence was regenerated for this summary.

The fixtures test source selection and presentation. Their fictitious identities do not establish real events involving vehicles visible in reused development footage. Historical model execution, artifact replay, synthetic fixtures and future real-model joint checks remain separate evidence classes.

## Supporting evidence

`evidence_manifest.csv` inventories the original 11 screenshots and four PDFs using their original relative paths, sizes and SHA-256 values. All 15 entries were checked against the retained originals and source index when preparing this text-only version.

Original artifacts are **not included**. Their public redistribution permission remains unconfirmed. Manifest paths identify retained originals; they are not links to files in this version. No screenshots, PDFs, video or original ZIP are copied here.

Supporting artifacts available for team review; links pending. No team sharing URL has been invented.

See [limitations.md](limitations.md) for missing evidence and NOT RUN items. This text-only preparation did not rerun validation, execute analysis, change code, create a branch, commit or push.
