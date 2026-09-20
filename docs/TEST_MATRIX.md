# Q60R test matrix

## Static / CI

- upstream commit pinned and reproducible
- application/package/service IDs unique to this distribution
- Tizen 5.0 packaging path selected
- AdBlock defaults enabled
- SponsorBlock defaults enabled
- JSON interception present
- upstream unit/service suite
- browser regression suite against Chromium (non-blocking)

## Physical TV

The following cannot be proven in GitHub Actions and must be run on the QE43Q60RATXXH:

1. App installation through the Tizen Homebrew path.
2. First launch and cold boot.
3. YouTube home/search/navigation.
4. Google account sign-in.
5. 1080p playback.
6. 4K playback where the source/TV path exposes it.
7. HDR if available for the selected video.
8. Remote control keys and seek/rewind behavior.
9. Ad-block behavior across pre-roll/mid-roll/navigation surfaces.
10. SponsorBlock auto-skip.
11. Suspend/resume and repeated launches.
12. Network loss/recovery.
13. TV reboot and service auto-start.
14. Coexistence with the official Samsung YouTube app.

## Acceptance principle

A feature is not considered hardware-verified until the physical TV passes the corresponding test. CI proves source/build invariants; it does not prove Samsung firmware behavior.
