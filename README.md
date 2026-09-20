# AdFree Tizen Q60R

Q60R-focused distribution/build wrapper for Samsung Tizen YouTube.

Target hardware:

- Model: `QE43Q60RATXXH`
- Generation: 2019
- Target platform: Tizen 5.0
- Known firmware from the target TV: `T-MSMDEUC-1500.9`

## Current validation state

The repository is being validated against the pinned upstream commit `9fb6ce97e6c430d55551aa47bfc924547d06909e` (version `1.4.0`). CI builds the dedicated Tizen 5.0 widget and runs upstream plus Q60R regression tests.

Hardware validation is separate and requires the physical QE43Q60RATXXH.

## Architecture

```text
Samsung Q60R
    -> Tizen app
       -> Cobalt / YouTube TV client
          -> upstream patch engine
             -> ad filtering
             -> SponsorBlock
             -> UI/player modifications
```

## License

The upstream project is GPL-3.0-only. This repository is a build/orchestration derivative and preserves upstream attribution and licensing.

Upstream: https://github.com/SushyDev/tizen-youtube

## Virtual validation

A dedicated Samsung Tizen 5.0 TV Emulator path is documented in `docs/EMULATOR_TIZEN5.md`.

Use TV Extension **5.0.1** and the Tizen 5.0 TV emulator. Samsung identifies the emulator platform as `tv-samsung-5.0-x86`; TV Extension 5.0.1 uses Chromium M63, matching the target software generation.

After the emulator is running, `scripts/emulator-smoke.sh` installs and launches the Q60R WGT through the Samsung Tizen CLI. This is a platform-level validation layer between CI and the physical TV.
