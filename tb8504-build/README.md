# Lenovo TB-8504F Android 16 cloud build lane

This branch is isolated from the Tizen project. It moves only safe, reproducible TB-8504F build work off the local PC.

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
7. optionally uploads the archive to a GitHub **draft release**;
8. writes a tiny public request file containing only the draft tag and seed ZIP SHA256.

The request commit triggers `.github/workflows/tb8504-boot.yml`.

The boot workflow refuses to repack until:

- the private draft seed downloads successfully;
- archive SHA256 matches the request;
- seed audit passes;
- boot header is legacy v0 / page size 2048 with TB8504 load addresses;
- the local kernel HEAD equals the pinned cloud kernel baseline;
- no local kernel patch, local kernel commit bundle or kernel untracked file remains unreconciled;
- the downloaded cloud kernel independently passes the current kernel auditor.

Only then does it replace the kernel in the proven boot seed.

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
