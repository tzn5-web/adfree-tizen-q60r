# Lenovo TB-8504F Android 16 cloud build lane

This branch is isolated from the Tizen project. It moves only safe, reproducible TB-8504F build work off the local PC.

## Primary operating model

The single supported local entry point is `scripts/tb8504-autopilot.py`.
For the provenance-only handoff, use the same entry point with `--goal converge --upload-seed`; this performs no Android image build and no device access.
The older `local-*.sh` runners are retained only as historical/support tooling;
they are not an alternate build procedure.

The autopilot is fail-closed and staged:

1. fetch one immutable tooling commit and its knowledge base;
2. verify source HEADs and source-integrity contracts;
3. apply only prevalidated idempotent source transforms;
4. rerun product, residual, ELF and runtime-contract audits;
5. compute and persist a source fingerprint;
6. bind boot/recovery/system/vendor/ROM artifacts to that fingerprint;
7. build only the next required stage;
8. audit the actual built output and final OTA package;
9. stop with a diagnostic bundle on an unknown condition.

It never performs adb, fastboot, flashing, block-device writes, clean,
installclean or clobber.

At the current audit point, final builds are intentionally blocked until the
local `hardware/qcom-caf/msm8996/gps` state and the post-convergence
`device/lenovo/TB8504` + `vendor/lenovo/TB8504` fingerprints have been
captured and explicitly accepted. `--goal converge` is the safe collection
step and performs no Android build or device access.

## Cloud scope

GitHub builds and audits:

- Linux 3.18 TB8504 kernel
- TB8504 DTB
- the expected 12 kernel modules
- Android 16 boot.img repack once a validated local seed is supplied

It never runs adb, fastboot, audio playback or any device write.

## Pinned kernel baseline

- repository: `lenovo-msm8917/kernel_lenovo_msm8917`
- baseline branch (documentation only): `lineage-21.0`
- exact commit: `d242d540d9f5328919e189235e74e433418f6d81`
- defconfig: `lineageos_tb8504_defconfig`
- toolchain commit is pinned in `config/sources.env`

The workflow fetches the exact commit SHA and checks out detached. It does not trust a moving branch head.

Kernel audit requires:

- TB8504 + msm8937 config
- ext4/fs encryption
- KEYS, AES, XTS, CTS, CBC and SHA256 crypto support
- forced module signing
- the expected 12 module names
- valid signed-module trailers
- the expected TB8504 DTB embedded in Image.gz-dtb
- partition-size safety

Because this legacy kernel auto-generates an X.509 module signing key for each clean build, signed kernel/module hashes can differ between otherwise identical runs. The audit records unsigned module hashes to distinguish real code changes from signing-key randomness. Security is not weakened just to force identical signed hashes.

## Android 16 seed bridge

The adapted Android 16 ramdisk/device state still originates from the already-built local LineageOS 23.2 workspace.

The seed is never committed to this public branch.

`scripts/export-cloud-seed.sh`:

1. exports source patches, local commits, untracked files and repo metadata;
2. includes the known modified repos, including GPS/LOC;
3. automatically discovers any additional dirty repo;
4. sanitizes remote URLs and skips sensitive untracked files;
5. includes the existing Android 16 boot.img seed;
6. generates SHA256SUMS;
7. optionally uploads the archive to a GitHub **draft release** when `tb8504-autopilot.py --goal converge --upload-seed` is used;
8. creates the tiny STAGE8N request as a Git commit whose direct parent is the exact pinned `TOOLING_REF`, then advances the branch only by fast-forward.

The request commit triggers `.github/workflows/tb8504-boot.yml`.

The boot workflow refuses to repack until:

- the private draft seed downloads successfully;
- archive SHA256 matches the request;
- seed audit passes;
- boot header is legacy v0 / page size 2048 with TB8504 load addresses;
- the local kernel HEAD equals the pinned cloud kernel baseline;
- no local runtime-relevant kernel divergence remains; the two exact audited build-only differences (`scripts/sign-file` and `include/sound/Kbuild`) may be classified explicitly rather than silently ignored;
- installed local modules are internally coherent with the local signing certificate;
- the downloaded cloud kernel independently passes the current kernel auditor;
- cloud/local signing identity must match before any cloud repack is allowed.

Only then would it replace the kernel in the proven boot seed. The current
cloud kernel uses a different module-signing identity than the canonical local
Android build, so this gate correctly blocks the repack. Final kernel, modules
and images therefore remain a local integrated-build responsibility.

## Boot repack invariants

`repack-boot.sh` pins LineageOS mkbootimg by exact commit and requires:

- boot header v0
- 2048-byte pages
- kernel address 0x80008000
- ramdisk address 0x81000000
- tags address 0x80000100
- no second-stage payload
- output <= 67,108,864 bytes
- repacked kernel SHA256 equals the cloud kernel SHA256
- ramdisk / second / recovery_dtbo / dtb are byte-identical to the seed where present
- header semantics remain unchanged apart from kernel-size/hash-derived fields

## Shared-kernel boot/recovery gate

Recovery and later goals build `bootimage recoveryimage` together when either
image needs refreshing, then re-audit both and compare their actual kernel
payload hashes. Building the images separately can regenerate the kernel
build timestamp, invalidating the boot image that passed earlier in the run.
A recovery-stage PASS requires a coherent pair, not only two earlier passes.

## Local image audit boundary

The local Android 16 identity gate accepts only `16` or `Baklava`, together
with SDK 36, `trunk_staging`, `lineage_TB8504`, and LineageOS 23.2.

The canonical boot ramdisk contains first-stage `init`. Device init scripts
are installed in `system/vendor/etc/init/hw`, so their IMS/runtime checks
remain mandatory in the built-system and final-package audits. The boot-only
audit checks the exact built AArch64 first-stage init and the embedded DTB's
system mount contract; it reports second-stage runtime validation as deferred.

Linux 3.18 modules use the legacy raw RSA signature format. The local auditor
parses that format, verifies every RSA/SHA512 signature using the local
kernel certificate, checks its key ID and vermagic, and requires installed
modules to match depmod staging. Modern `modinfo` is not used to infer that
these legacy signatures are missing.

Regression checks cover invalid release identities, missing or corrupted
first-stage evidence, disabled or changed system mount contracts, removed
service controls, missing signatures, and altered signed payloads.

## Recovered crDroid 10 golden profile

The captured ROM is `crDroidAndroid-10.0-20211226-TB8504-v6.25`,
Android 10 / SDK 29; the label describes Android 10, not crDroid version 10.
The original 2026-10-03 capture is retained locally in
`android16-tb8504/golden` and the original `perf-baseline` folder.
`apply-golden-profile.py` records the capture SHA256 and appends a guarded
TB8504-only override after Qualcomm's post-boot setup. It restores the
observed interactive governor values, core_ctl setting, swappiness,
page-cluster, CFQ/read-ahead and Adreno governor/default power level.
The inherited 1 GiB ZRAM configuration and six Dalvik heap settings match
the capture. Built-output auditing requires the final golden script and
those heap settings. Thermal controls remain active.

Transient frequencies, memory-use measurements, old ART optimization modes,
and Android 10 LMK policy are not treated as portable Android 16 settings.
Runtime application/performance still requires a physical device test;
source and image audit success alone cannot establish this.

Any source change invalidates prior image bindings. The first golden-profile
run must use `--goal converge --upload-seed`, pass STAGE8N, and promote the
new provenance before the next staged build is accepted.

## Accepted golden source and paired images

The golden convergence seed `tb8504-cloud-seed-20261009_114026` passed
[STAGE8N run 37906549460](https://github.com/tzn5-web/adfree-tizen-q60r/actions/runs/37906549460).
The audited source hashes were promoted in commit
`b4ab0f1` before local image builds resumed. This is source acceptance;
physical-device performance validation remains pending.

The local orchestrator requires successful lint on its exact tooling commit.
GitHub suppresses push-triggered workflows for commits made with
`GITHUB_TOKEN`, including the automatic provenance acceptance commit.
After that promotion, a reviewed tooling or documentation commit must pass
push lint before starting the next local stage; an earlier lint run is not
accepted as evidence for the new commit.

Boot and recovery use one combined `mka bootimage recoveryimage` build when
either image is stale. Their embedded kernel payload hashes must match after
both image audits. Separate successful image audits do not prove that the
pair has the same kernel. System and ROM stages reuse this same pair gate.

## Physical layout

- boot: 67,108,864 bytes
- recovery: 67,108,864 bytes
- system: 4,080,218,112 bytes
- legacy static partitions
- non-A/B
- vendor inside system (`system/vendor`)

## What stays local

The PC/tablet is still required for:

- physical partition/GPT backup
- temporary recovery boot
- actual flashing
- first Android 16 boot
- adb/logcat/dmesg runtime validation
- display/touch/Wi-Fi/GPS/video/audio hardware testing

No cloud workflow performs those operations.


## Frozen Android16 configuration and legacy VINTF policy

Canonical builds select `bp4a`, the release selected by the pinned
`vendor/lineage/vars/aosp_target_release`, with SDK36 and frozen AIDL.
`trunk_staging` selects unfrozen interfaces and the next-release FCM set in
this source tree and is no longer accepted for canonical images. Release
selection participates in the source fingerprint and invalidates previous
image bindings while preserving compilation caches.

The device matrix retains implemented Bluetooth audio HIDL2.0 and supports
both frozen/current AIDL versions for DRM1-2, Wi-Fi3-4, and Supplicant4-5.
Exact file and full Git patch pre/post hashes guard this one-file mutation;
source HEADs, vendor payloads, and the declared target FCM5 remain unchanged.

The built-output checker reads the successfully queried build policy and
cross-checks shipping API against installed `ro.product.first_api_level=25`.
It follows the pinned AOSP `product_config.mk` / `core/Makefile`: kernel VINTF
requirements are mandatory by default for devices launched with API29+,
and are optional for older devices. An enabled flag is always enforced;
missing/unknown policy, duplicate fields, or an unverified API are rejected.
For the verified API25 legacy build with an unset flag, HAL compatibility
must still pass. The additional kernel VINTF diagnostic return code is
recorded separately and is never represented as a successful kernel check.
Signed module/kernel/boot/recovery coherence gates remain mandatory. Actual
kernel runtime compatibility and the golden profile require device tests.
AOSP policy: https://source.android.com/docs/core/architecture/vintf/dm
