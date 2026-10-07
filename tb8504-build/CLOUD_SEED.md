# One-time local-to-cloud bridge

The Android 16 source modifications currently live in the local LineageOS 23.2 workspace. GitHub must not guess or replace that state with an older public ramdisk.

## Export

Run `scripts/export-cloud-seed.sh` on the local workspace. It does **no build** and **no device access**.

The archive contains:

- the adapted TB8504 device tree
- working-tree binary patches
- local commit bundles for the curated modified repos
- untracked non-sensitive files
- full repo status inventory
- sanitized repo metadata
- existing Android 16 boot.img and optional recovery.img
- SHA256SUMS

Known relevant repos explicitly include device, vendor, kernel, Qualcomm audio/media/display/GPS, Lineage compat and legacy Qualcomm SELinux. Additional dirty repo projects are discovered automatically.

The exporter now distinguishes **requested** repositories from repositories
that were actually captured. Every requested repo must be a readable Git
worktree; otherwise export fails. `EXPORTED_REPOS.txt` contains only captured
repos, and the seed auditor requires a one-to-one match with `REPOS.txt` plus
the per-repo status, patch, commit-bundle and upstream metadata.

Sensitive untracked filenames such as private keys, keystores, tokens, credentials and .env files are skipped and reported.

## Audit

`scripts/audit-cloud-seed.py` rejects:

- unsafe ZIP/tar paths
- duplicate ZIP entries
- incomplete SHA256 coverage
- credential-like text leakage
- sensitive filenames
- archive expansion beyond safety limits
- a missing boot seed
- wrong boot magic/header/page size/load addresses
- missing kernel HEAD metadata
- an export that omitted Qualcomm GPS/LOC
- a requested repository that was declared but not actually captured
- any mismatch between exported repo metadata and its per-repo patch/status/mbox/upstream files

It also reports the exact local kernel HEAD and whether the kernel has working-tree patches, local commit bundles or untracked files.

The seed `tb8504-cloud-seed-20261007_082702` predates this hardening. It is
still useful as the historical source for the completed device/vendor STAGE8N
audit, but it is **not accepted as complete final provenance**, because
`hardware/qcom-caf/msm8996/gps` was listed in the requested/exported set
without corresponding captured repo metadata. A new seed must be generated
after the local autopilot records the actual GPS state.

## Private GitHub transport

The binary seed is not committed to the public repository.

With `TB8504_UPLOAD_DRAFT=1`, the exporter uses an authenticated `gh` CLI to:

1. upload the seed ZIP to a GitHub draft release;
2. compute its SHA256;
3. commit a small `tb8504-build/requests/boot-*.txt` request to the isolated branch.

The request contains only:

- the draft release tag
- the seed ZIP SHA256

That request triggers the boot workflow. Draft release data remains unpublished.

If `gh` is unavailable or unauthenticated, the archive still remains on the Windows Desktop and can be inspected separately; no public fallback upload is attempted.

## Reconciliation rule

A local kernel divergence is a hard stop. GitHub does not silently replace a modified local kernel with the public baseline.

If the seed reports any kernel patch/commit/untracked state, that difference is reviewed and either reproduced in the cloud kernel lane or explicitly proven build-only/non-runtime before boot repack proceeds.

Physical flashing remains local-only.
