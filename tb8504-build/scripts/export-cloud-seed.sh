#!/usr/bin/env bash

# One-time bridge from the already-adapted local Android 16 tree to GitHub.
# No build, no adb, no fastboot, no device writes.

ROOT="${1:-$HOME/android16-tb8504/lineage-23.2}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$ROOT/TB8504_CLOUD_SEED_$STAMP"
ZIP_NAME="TB8504_CLOUD_SEED_$STAMP.zip"

mkdir -p "$OUT/patches" "$OUT/commits" "$OUT/untracked" "$OUT/meta" "$OUT/seeds"
RC=0

log() { printf '%s\n' "$*"; }
warn() { log "WARN=$*"; }
fail() { log "FAIL=$*"; RC=2; }

if [ ! -d "$ROOT/device/lenovo/TB8504" ]; then
    fail "device tree missing under $ROOT"
fi

REPOS=(
    "device/lenovo/TB8504"
    "vendor/lenovo/TB8504"
    "kernel/lenovo/msm8917"
    "hardware/qcom-caf/msm8996/audio"
    "hardware/qcom-caf/msm8996/media"
    "hardware/qcom-caf/msm8996/display"
    "hardware/lineage/compat"
    "device/qcom/sepolicy-legacy-um"
)

: > "$OUT/meta/REPOS.txt"

for REL in "${REPOS[@]}"; do
    DIR="$ROOT/$REL"
    KEY="${REL//\//__}"

    if ! git -C "$DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        warn "not a git worktree: $REL"
        continue
    fi

    HEAD_SHA="$(git -C "$DIR" rev-parse HEAD 2>/dev/null)"
    REMOTE="$(git -C "$DIR" remote get-url origin 2>/dev/null)"
    BRANCH="$(git -C "$DIR" branch --show-current 2>/dev/null)"

    {
        echo "REPO=$REL"
        echo "HEAD=$HEAD_SHA"
        echo "BRANCH=$BRANCH"
        echo "REMOTE=$REMOTE"
        echo
    } >> "$OUT/meta/REPOS.txt"

    git -C "$DIR" status --short > "$OUT/meta/$KEY.status.txt" 2>&1
    git -C "$DIR" diff --binary HEAD > "$OUT/patches/$KEY.patch" 2>&1

    # Preserve committed local work too. git diff HEAD alone cannot see
    # commits that exist only in this workspace.
    UPSTREAM="$(git -C "$DIR" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"
    if [ -z "$UPSTREAM" ] && [ -n "$BRANCH" ]; then
        if git -C "$DIR" show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
            UPSTREAM="origin/$BRANCH"
        fi
    fi

    if [ -n "$UPSTREAM" ]; then
        BASE_SHA="$(git -C "$DIR" merge-base HEAD "$UPSTREAM" 2>/dev/null)"
        if [ -n "$BASE_SHA" ] && [ "$BASE_SHA" != "$HEAD_SHA" ]; then
            git -C "$DIR" format-patch --binary --stdout "$BASE_SHA..HEAD" \
                > "$OUT/commits/$KEY.mbox" 2> "$OUT/meta/$KEY.format-patch.stderr.txt"
            FP_RC=$?
            if [ "$FP_RC" -ne 0 ]; then
                warn "format-patch failed for $REL rc=$FP_RC"
            fi
        else
            : > "$OUT/commits/$KEY.mbox"
        fi
        {
            echo "UPSTREAM=$UPSTREAM"
            echo "BASE=$BASE_SHA"
            echo "HEAD=$HEAD_SHA"
        } > "$OUT/meta/$KEY.upstream.txt"
    else
        warn "no tracking upstream for $REL; HEAD metadata + working-tree diff exported"
        {
            echo "UPSTREAM="
            echo "BASE="
            echo "HEAD=$HEAD_SHA"
        } > "$OUT/meta/$KEY.upstream.txt"
    fi

    while IFS= read -r -d '' F; do
        SRC="$DIR/$F"
        DST="$OUT/untracked/$REL/$F"
        mkdir -p "$(dirname "$DST")"
        cp -a "$SRC" "$DST"
    done < <(git -C "$DIR" ls-files --others --exclude-standard -z 2>/dev/null)
done

# Full device tree is small and is the most important ramdisk/product input.
tar -czf "$OUT/device_lenovo_TB8504.tar.gz"     -C "$ROOT/device/lenovo"     --exclude='.git'     TB8504 2>/dev/null || fail "device tree tar failed"

if [ -d "$ROOT/.repo/local_manifests" ]; then
    tar -czf "$OUT/local_manifests.tar.gz"         -C "$ROOT/.repo" local_manifests 2>/dev/null || warn "local manifests tar failed"
fi

PRODUCT_OUT="$ROOT/out/target/product/TB8504"

if [ -f "$PRODUCT_OUT/boot.img" ]; then
    cp -f "$PRODUCT_OUT/boot.img" "$OUT/seeds/boot_seed.img"
    echo "BOOT_SEED=present" > "$OUT/meta/SEEDS.txt"
else
    echo "BOOT_SEED=missing" > "$OUT/meta/SEEDS.txt"
    warn "Android 16 boot.img seed not found at $PRODUCT_OUT/boot.img"
fi

if [ -f "$PRODUCT_OUT/recovery.img" ]; then
    cp -f "$PRODUCT_OUT/recovery.img" "$OUT/seeds/recovery_seed.img"
    echo "RECOVERY_SEED=present" >> "$OUT/meta/SEEDS.txt"
else
    echo "RECOVERY_SEED=missing" >> "$OUT/meta/SEEDS.txt"
fi

for F in     "$PRODUCT_OUT/module-info.json"     "$PRODUCT_OUT/system/build.prop"     "$PRODUCT_OUT/system/system/build.prop"
do
    if [ -f "$F" ]; then
        cp -f "$F" "$OUT/meta/$(basename "$(dirname "$F")")_$(basename "$F")" 2>/dev/null || true
    fi
done

{
    echo "EXPORT_ROOT=$ROOT"
    echo "CREATED_AT=$(date -Iseconds)"
    echo "NO_BUILD=YES"
    echo "NO_FLASH=YES"
    echo "EXPECTED_BOOT_SIZE=67108864"
    echo "EXPECTED_RECOVERY_SIZE=67108864"
    echo "EXPECTED_SYSTEM_SIZE=4080218112"
} > "$OUT/meta/EXPORT.txt"

(
    if cd "$OUT"; then
        find . -type f ! -name SHA256SUMS -print0 |
            sort -z |
            xargs -0 -r sha256sum > SHA256SUMS
    else
        warn "cannot enter export directory for SHA256SUMS"
    fi
)

WIN_DESKTOP="$(
    powershell.exe -NoProfile -Command '[Environment]::GetFolderPath("Desktop")' 2>/dev/null |
    tr -d '\r'
)"
DESKTOP="$(wslpath -u "$WIN_DESKTOP" 2>/dev/null)"

if [ -z "$DESKTOP" ] || [ ! -d "$DESKTOP" ]; then
    DESKTOP="$HOME/Desktop"
    mkdir -p "$DESKTOP"
fi

ARCHIVE="$DESKTOP/$ZIP_NAME"

python3 - "$OUT" "$ARCHIVE" <<'PY'
from pathlib import Path
import sys
import zipfile

src = Path(sys.argv[1])
dst = Path(sys.argv[2])

with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for p in sorted(src.rglob("*")):
        if p.is_file():
            z.write(p, p.relative_to(src))

with zipfile.ZipFile(dst, "r") as z:
    bad = z.testzip()
    if bad:
        raise SystemExit("corrupt zip entry: " + bad)

print("ARCHIVE_INTEGRITY=PASS")
PY
ZIP_RC=$?

if [ "$ZIP_RC" -ne 0 ]; then
    fail "archive creation/integrity failed rc=$ZIP_RC"
fi

echo
echo "CLOUD_SEED_ARCHIVE=$ARCHIVE"
echo "FINAL_RC=$RC"
echo "NO_BUILD=YES"
echo "NO_FLASH=YES"
exit "$RC"
