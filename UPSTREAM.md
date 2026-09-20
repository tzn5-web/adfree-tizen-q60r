# Upstream provenance

Project: `SushyDev/tizen-youtube`

Pinned commit: `9fb6ce97e6c430d55551aa47bfc924547d06909e`

Pinned commit date: 2026-09-18

Upstream version: `1.4.0`

License: GPL-3.0-only

## Why this base was selected

The upstream project has a dedicated Tizen 5.0 package path, uses the Samsung/Cobalt playback architecture, contains an ad-block implementation at the response/request layer, integrates SponsorBlock, and carries automated tests for its service, mods and browser path.

This project intentionally does not track a floating `main` during builds. The commit is pinned so that a YouTube or upstream change cannot silently alter the binary we test for the Q60R.

## Update policy

Changing `UPSTREAM_COMMIT` is a deliberate compatibility change. CI must pass the Q60R regression suite before the commit is accepted.
