# Samsung Tizen 5.0 virtual test environment

## Target

This project targets the 2019 Q60R family on Tizen 5.0. Samsung's TV Extension 5.0.1 includes a Tizen 5.0 TV Emulator, and Samsung documents the emulator platform as `tv-samsung-5.0-x86`.

For the Q60R target, use TV Extension **5.0.1** rather than a newer TV Extension. Samsung's 5.0.1 release notes state that the Tizen 5.0 emulator uses Chromium **M63**, matching the generation recorded for the target TV.

Samsung archive:
https://developer.samsung.com/smarttv/develop/tools/tv-extension/archive.html

Official release notes:
https://developer.samsung.com/smarttv/develop/tools/tv-extension/release-history.html

## Host requirements

Samsung documents hardware-assisted virtualization and OpenGL ES acceleration for the TV Emulator. The emulator is a virtual machine based on QEMU.

Recommended host characteristics:
- 64-bit Windows, Ubuntu, or Intel-based macOS
- CPU virtualization enabled
- GPU/OpenGL ES acceleration available
- at least 4 GB host RAM and 6 GB free disk for the SDK; the emulator itself should be configured with at least 1024 MB RAM
- do not run the Samsung TV Emulator inside VirtualBox, VMware, Parallels, or Remote Desktop according to Samsung's emulator prerequisites

## Install

Samsung's documented flow is:

1. Install the compatible Tizen Studio release.
2. In Package Manager, install the Samsung TV Extension 5.0.1 image from the Extension SDK/local-image route.
3. Open **Tools -> Emulator Manager**.
4. Create a **TV** emulator instance.
5. Select the Tizen 5.0 TV image/template.
6. Verify CPU VT and GPU acceleration are enabled.
7. Launch the emulator.
8. Install the generated `tube-tizen-5.0.wgt`.

The target emulator platform referenced by Samsung examples is:

`tv-samsung-5.0-x86`

Samsung also documents a virtual remote and bridged networking, which are useful for this project's navigation and YouTube/network tests.

## CLI smoke test

After starting the emulator, Samsung documents these Tizen CLI operations for a TV emulator: retrieve the target with SDB, grant installation permission, install the WGT, and run the application by application ID. The repository now provides `scripts/emulator-smoke.sh` to execute that sequence.

Example:

```bash
TIZEN_HOME=/path/to/tizen-studio \\
WGT=/path/to/tube-tizen-5.0.wgt \\
CERT_PROFILE=myCert \\
bash ./scripts/emulator-smoke.sh
```

The default target is `emulator-26101` and the default application ID is `Q60AdFree1.Tube`. Both can be overridden with `SERIAL` and `APP_ID`.

## What this can validate

The emulator is suitable for high-value pre-hardware checks:

- WGT installation and application launch
- Tizen 5.0 configuration and permissions
- Cobalt/Chromium M63 startup
- navigation and remote key handling
- JavaScript/runtime startup
- AdBlock patch initialization
- SponsorBlock initialization
- authentication flow where the emulator permits it
- network connectivity and request failures
- application lifecycle and suspend/resume
- Web Inspector/debug logs

## What it cannot prove

Do not mark the Q60R hardware as verified based on emulator results.

Samsung documents emulator limitations and differences from real TVs. In particular, hardware-specific behavior, DRM/media paths, 4K/HDR behavior, and some product APIs can differ or be unavailable.

The final acceptance gate remains the physical `QE43Q60RATXXH`.

## Project-specific smoke test

After launching the emulator, use the Q60R widget built by CI:

`tube-tizen-5.0.wgt`

The CI WGT is intentionally unsigned for the physical-TV/Homebrew workflow. Samsung's CLI requires a valid certificate profile for test packaging, so `scripts/emulator-smoke.sh` extracts the WGT, signs a temporary copy with `tizen package -t wgt -s <profile>`, installs that signed copy, and launches `Q60AdFree1.Tube`. Samsung documents that valid certificate profiles are required for CLI packaging/testing. citeturn292355view0turn292355search1

Record at minimum:

1. install success
2. first launch success
3. home/search navigation
4. playback start
5. 1080p playback
6. remote seek/rewind
7. ad-block behavior
8. SponsorBlock behavior
9. suspend/resume
10. restart after emulator reboot

For ad filtering, the important observation is not only "no visible ad". Capture startup and player behavior as well, because a false positive can also present as a broken playback path.

## Important distinction

There are three different test layers in this repository:

```text
CI Chromium
    -> fast JS/browser regression

Samsung Tizen 5.0 TV Emulator
    -> Tizen platform + Chromium M63 + virtual TV controls

Physical QE43Q60RATXXH
    -> real firmware + real hardware + real media/DRM path
```

Passing the first layer does not substitute for the second. Passing the second does not substitute for the third.
