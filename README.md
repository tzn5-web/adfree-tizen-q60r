# AdFree Tizen Q60R

Q60R-focused distribution/build wrapper for Samsung Tizen YouTube.

Target hardware:

- Model: `QE43Q60RATXXH`
- Generation: 2019
- Target platform: Tizen 5.0
- Known firmware from the target TV: `T-MSMDEUC-1500.9`

## Architecture

This repository does not reimplement a video decoder or a YouTube extractor. It pins a tested upstream Tizen YouTube implementation and builds the dedicated Tizen 5.0 widget, applying a small Q60R overlay:

```text
Samsung Q60R
    -> Tizen app
       -> Cobalt / YouTube TV client
          -> upstream patch engine
             -> ad filtering
             -> SponsorBlock
             -> UI/player modifications
```

The pinned upstream is `SushyDev/tizen-youtube` at commit `9fb6ce97e6c430d55551aa47bfc924547d06909e` (upstream version 1.4.0 at the time this project was created).

## What the build does

1. Downloads the exact upstream commit.
2. Verifies the expected project identity and ad-block components.
3. Applies the Q60R application/package identity.
4. Builds the userscript and both service bundles using the upstream toolchain.
5. Packages the Tizen 5.0 widget.
6. Runs upstream unit/integration tests where GitHub Actions permits them.
7. Runs Q60R-specific regression tests against the staged source.
8. Publishes the unsigned `.wgt` as a CI artifact.

The widget is intentionally unsigned. Samsung/Tizen Homebrew signs the package for the target TV.

## Install path

The first hardware test should keep the official Samsung YouTube app installed. Install the generated `adfree-tizen-q60r-*-tizen5.0.wgt` side-by-side, validate playback/login/remote control, then decide whether the stock app is still needed.

## Legal / upstream

The upstream project is GPL-3.0-only. This repository contains the build/orchestration layer for that project and keeps the upstream commit pinned and attributed. The produced application is a derivative work of the GPL upstream and must be distributed consistently with its license.

Upstream: https://github.com/SushyDev/tizen-youtube
