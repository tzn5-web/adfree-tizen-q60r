# Lenovo TB-8504F Android 16 cloud build lane

This branch is isolated from the Tizen project. It exists only to move safe, reproducible TB-8504F build work off the local PC.

## What is real in GitHub now

The workflow builds the TB8504 Linux 3.18 kernel, the TB8504 DTB and kernel modules from pinned public sources. It then audits the produced artifacts and uploads them as a GitHub Actions artifact. It never flashes a device.

Pinned kernel:
- `lenovo-msm8917/kernel_lenovo_msm8917`
- branch `lineage-21.0`
- commit `d242d540d9f5328919e189235e74e433418f6d81`
- defconfig `lineageos_tb8504_defconfig`

The defconfig is required to contain TB8504, msm8937 and ext4/fs encryption support.

## Why boot.img and recovery.img are gated

The current Android 16 device tree/ramdisk modifications live in the local LineageOS 23.2 tree and are not yet present in GitHub. Building `boot.img` or `recovery.img` from an old public ramdisk would create an artifact that looks valid but is not the Android 16 port we audited locally.

Therefore this cloud lane does **not** fabricate boot/recovery images. The next cloud stage is enabled only after the adapted Android 16 device/ramdisk state is reproduced here.

Known physical layout:
- boot: 67,108,864 bytes
- recovery: 67,108,864 bytes
- system: 4,080,218,112 bytes
- legacy static partitions
- non-A/B
- vendor inside system (`system/vendor`)

## Safety rules

- no fastboot
- no adb
- no device writes
- no audio playback
- source commits are pinned
- SHA256 and sizes are recorded
- missing DTB/config/modules are hard failures
- a kernel payload at or above the boot partition size is a hard failure

Local PC remains required for physical backup, temporary recovery boot, flashing, first boot and hardware validation.
