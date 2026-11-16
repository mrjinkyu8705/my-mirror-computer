# Release notes

## 2026-11-16

- H.264: after a loss-related slowdown, a brief scroll or window transition
  remains eligible for a bounded capacity probe for up to three seconds.
  Existing feedback, loss handling, and bitrate limits still apply.
- Includes changes to video recovery, pacing, input timing, clipboard,
  and file-transfer handling.
- The public build requires your own signaling URL and excludes deployment
  secrets, media captures, and operational backups.
