# One-time local-to-cloud bridge

The Android 16 source modifications currently live in the local LineageOS 23.2 workspace. GitHub must not guess or replace that state with an older public ramdisk.

Use `scripts/export-cloud-seed.sh` once on the local workspace. It performs **no build** and **no device access**. It exports source diffs, untracked files, the adapted device tree, metadata and—if already present—the existing Android 16 boot/recovery images as seed inputs.

Before anything from the archive is admitted to the GitHub build lane, run `scripts/audit-cloud-seed.py`. The audit verifies archive paths, all exported SHA256 values, partition-size metadata, Android boot magic and device-tree tar safety.

Binary boot/recovery seeds are intentionally **not automatically committed to this public repository**. Source changes and binary seed handling are reviewed separately.

After a clean seed audit:
1. source diffs/device tree are reconciled with the cloud lane;
2. the cloud-built kernel remains the authoritative kernel artifact;
3. `repack-boot.sh` replaces only the kernel in the proven Android 16 boot seed;
4. ramdisk/DTB/second/recovery_dtbo components must remain byte-identical;
5. physical flashing remains local-only.
