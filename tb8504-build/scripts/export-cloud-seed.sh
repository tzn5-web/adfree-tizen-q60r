#!/usr/bin/env bash

# One-time bridge from the already-adapted Android 16 tree to GitHub.
# No build, no adb, no fastboot, no device writes.

set -o pipefail

ROOT="${1:-$HOME/android16-tb8504/lineage-23.2}"
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$ROOT/TB8504_CLOUD_SEED_$STAMP"
ZIP_NAME="TB8504_CLOUD_SEED_$STAMP.zip"
UPLOAD_DRAFT="${TB8504_UPLOAD_DRAFT:-0}"
GITHUB_REPO="${TB8504_GITHUB_REPO:-tzn5-web/adfree-tizen-q60r}"
GITHUB_TARGET="${TB8504_GITHUB_TARGET:-tb8504-android16-build}"

mkdir -p "$OUT/patches" "$OUT/commits" "$OUT/untracked" "$OUT/meta" "$OUT/seeds" "$OUT/installed-modules"
RC=0

log() { printf '%s\n' "$*"; }
warn() { log "WARN=$*"; }
fail() { log "FAIL=$*"; RC=2; }

sanitize_remote() {
    python3 - "$1" <<'PY_SANITIZE'
from urllib.parse import urlsplit, urlunsplit
import sys

raw = sys.argv[1]
try:
    p = urlsplit(raw)
    if p.scheme and p.hostname:
        host = p.hostname
        if p.port:
            host += f":{p.port}"
        print(urlunsplit((p.scheme, host, p.path, p.query, p.fragment)))
    else:
        print(raw)
except Exception:
    print("")
PY_SANITIZE
}

sensitive_path() {
    local p="${1,,}"
    case "$p" in
        *.pem|*.key|*.p12|*.pfx|*.jks|*.keystore|*.mobileprovision|        */id_rsa|*/id_ed25519|id_rsa|id_ed25519|        *.env|*/.env|*credentials*|*credential*|*secret*|*token*)
            return 0
            ;;
    esac
    return 1
}

if [ ! -d "$ROOT/device/lenovo/TB8504" ]; then
    fail "device tree missing under $ROOT"
fi

BASE_REPOS=(
    "device/lenovo/TB8504"
    "vendor/lenovo/TB8504"
    "kernel/lenovo/msm8917"
    "hardware/qcom-caf/msm8996/audio"
    "hardware/qcom-caf/msm8996/media"
    "hardware/qcom-caf/msm8996/display"
    "hardware/qcom-caf/msm8996/gps"
    "hardware/lineage/compat"
    "device/qcom/sepolicy-legacy-um"
)

REPOS=()
declare -A SEEN_REPOS=()

add_repo() {
    local rel="$1"
    [ -n "$rel" ] || return 0
    if [ -z "${SEEN_REPOS[$rel]+x}" ]; then
        SEEN_REPOS["$rel"]=1
        REPOS+=("$rel")
    fi
}

for REL in "${BASE_REPOS[@]}"; do
    add_repo "$REL"
done

# Discover any additional modified repo automatically. This catches fixes that
# were made outside the manually curated list.
REPO_BIN="$ROOT/.repo/repo/repo"
if [ -x "$REPO_BIN" ]; then
    while IFS= read -r REL; do
        [ -n "$REL" ] || continue
        DIR="$ROOT/$REL"
        git -C "$DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1 || continue

        DIRTY="$(git -C "$DIR" status --porcelain --untracked-files=all 2>/dev/null)"
        if [ -n "$DIRTY" ]; then
            add_repo "$REL"
        fi
    done < <(cd "$ROOT" && "$REPO_BIN" list -p 2>/dev/null)
else
    warn "repo launcher unavailable; using curated repo list"
fi

if [ -x "$REPO_BIN" ]; then
    (cd "$ROOT" && "$REPO_BIN" status) > "$OUT/meta/REPO_STATUS_FULL.txt" 2>&1 || true
fi

printf '%s\n' "${REPOS[@]}" | sort -u > "$OUT/meta/EXPORTED_REPOS.txt"
: > "$OUT/meta/REPOS.txt"
: > "$OUT/meta/SKIPPED_SENSITIVE_FILES.txt"

for REL in "${REPOS[@]}"; do
    DIR="$ROOT/$REL"
    KEY="${REL//\//__}"

    if ! git -C "$DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        warn "not a git worktree: $REL"
        continue
    fi

    HEAD_SHA="$(git -C "$DIR" rev-parse HEAD 2>/dev/null)"
    REMOTE_RAW="$(git -C "$DIR" remote get-url origin 2>/dev/null)"
    REMOTE="$(sanitize_remote "$REMOTE_RAW")"
    BRANCH="$(git -C "$DIR" branch --show-current 2>/dev/null)"

    {
        echo "REPO=$REL"
        echo "HEAD=$HEAD_SHA"
        echo "BRANCH=$BRANCH"
        echo "REMOTE=$REMOTE"
        echo
    } >> "$OUT/meta/REPOS.txt"

    git -C "$DIR" status --short --untracked-files=all         > "$OUT/meta/$KEY.status.txt" 2>&1
    git -C "$DIR" diff --binary HEAD > "$OUT/patches/$KEY.patch" 2>&1

    UPSTREAM="$(git -C "$DIR" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)"
    if [ -z "$UPSTREAM" ] && [ -n "$BRANCH" ]; then
        if git -C "$DIR" show-ref --verify --quiet "refs/remotes/origin/$BRANCH"; then
            UPSTREAM="origin/$BRANCH"
        fi
    fi

    if [ -n "$UPSTREAM" ]; then
        BASE_SHA="$(git -C "$DIR" merge-base HEAD "$UPSTREAM" 2>/dev/null)"
        if [ -n "$BASE_SHA" ] && [ "$BASE_SHA" != "$HEAD_SHA" ]; then
            git -C "$DIR" format-patch --binary --stdout "$BASE_SHA..HEAD"                 > "$OUT/commits/$KEY.mbox"                 2> "$OUT/meta/$KEY.format-patch.stderr.txt"
            FP_RC=$?
            if [ "$FP_RC" -ne 0 ]; then
                warn "format-patch failed for $REL rc=$FP_RC"
            fi
        else
            : > "$OUT/commits/$KEY.mbox"
        fi
    else
        # If no tracking branch exists, preserve local-only commits that are
        # not reachable from any origin remote ref.
        LOCAL_COMMITS="$(
            git -C "$DIR" rev-list --reverse HEAD --not --remotes=origin 2>/dev/null
        )"
        if [ -n "$LOCAL_COMMITS" ]; then
            FIRST_LOCAL="$(printf '%s\n' "$LOCAL_COMMITS" | head -n1)"
            BASE_SHA="$(git -C "$DIR" rev-parse "$FIRST_LOCAL^" 2>/dev/null)"
            if [ -n "$BASE_SHA" ]; then
                git -C "$DIR" format-patch --binary --stdout "$BASE_SHA..HEAD"                     > "$OUT/commits/$KEY.mbox"                     2> "$OUT/meta/$KEY.format-patch.stderr.txt"
            else
                : > "$OUT/commits/$KEY.mbox"
            fi
        else
            BASE_SHA=""
            : > "$OUT/commits/$KEY.mbox"
        fi
    fi

    {
        echo "UPSTREAM=$UPSTREAM"
        echo "BASE=$BASE_SHA"
        echo "HEAD=$HEAD_SHA"
    } > "$OUT/meta/$KEY.upstream.txt"

    while IFS= read -r -d '' F; do
        if sensitive_path "$F"; then
            echo "$REL/$F" >> "$OUT/meta/SKIPPED_SENSITIVE_FILES.txt"
            warn "sensitive untracked file skipped: $REL/$F"
            continue
        fi

        SRC="$DIR/$F"
        DST="$OUT/untracked/$REL/$F"
        mkdir -p "$(dirname "$DST")"
        cp -a "$SRC" "$DST"
    done < <(git -C "$DIR" ls-files --others --exclude-standard -z 2>/dev/null)
done

# Full device tree is small and contains the Android 16 ramdisk/product source.
tar -czf "$OUT/device_lenovo_TB8504.tar.gz"     -C "$ROOT/device/lenovo"     --exclude='.git'     TB8504 2>/dev/null || fail "device tree tar failed"

if [ -d "$ROOT/.repo/local_manifests" ]; then
    tar -czf "$OUT/local_manifests.tar.gz"         -C "$ROOT/.repo" local_manifests 2>/dev/null         || warn "local manifests tar failed"
fi

PRODUCT_OUT="$ROOT/out/target/product/TB8504"

# Preserve the exact installed kernel-module state from the Android 16 product.
# This is required because CONFIG_MODULE_SIG_FORCE=y: a cloud kernel built with
# a different X.509 key must never be paired blindly with locally signed modules.
KOBJ="$PRODUCT_OUT/obj/KERNEL_OBJ"
: > "$OUT/meta/INSTALLED_MODULES.txt"

MODULE_COUNT=0
for MODROOT in \
    "$PRODUCT_OUT/system" \
    "$PRODUCT_OUT/vendor" \
    "$PRODUCT_OUT/product" \
    "$PRODUCT_OUT/system_ext" \
    "$PRODUCT_OUT/root" \
    "$PRODUCT_OUT/recovery/root"
do
    [ -d "$MODROOT" ] || continue

    while IFS= read -r -d '' MOD; do
        REL="${MOD#$PRODUCT_OUT/}"
        DST="$OUT/installed-modules/$REL"
        mkdir -p "$(dirname "$DST")"
        cp -f "$MOD" "$DST"

        SIZE="$(stat -c '%s' "$MOD" 2>/dev/null)"
        SHA="$(sha256sum "$MOD" | awk '{print $1}')"
        printf '%s\t%s\t%s\n' "$REL" "$SIZE" "$SHA" >> "$OUT/meta/INSTALLED_MODULES.txt"
        MODULE_COUNT=$((MODULE_COUNT + 1))
    done < <(find "$MODROOT" -type f -name '*.ko' -print0 2>/dev/null)
done

sort -u -o "$OUT/meta/INSTALLED_MODULES.txt" "$OUT/meta/INSTALLED_MODULES.txt"
MODULE_COUNT="$(wc -l < "$OUT/meta/INSTALLED_MODULES.txt")"
echo "INSTALLED_MODULE_COUNT=$MODULE_COUNT" > "$OUT/meta/MODULE_SIGNING.txt"

if [ -f "$KOBJ/.config" ]; then
    cp -f "$KOBJ/.config" "$OUT/meta/local_kernel.config"
fi

if [ -f "$KOBJ/signing_key.x509" ]; then
    cp -f "$KOBJ/signing_key.x509" "$OUT/meta/local_module_signing.x509"
    echo "LOCAL_MODULE_SIGNING_CERT=present" >> "$OUT/meta/MODULE_SIGNING.txt"
else
    echo "LOCAL_MODULE_SIGNING_CERT=missing" >> "$OUT/meta/MODULE_SIGNING.txt"
    if [ "$MODULE_COUNT" -gt 0 ]; then
        warn "installed modules found but local public module-signing certificate is missing"
    fi
fi

# Never export the private signing key.
if [ -e "$KOBJ/signing_key.priv" ]; then
    echo "LOCAL_PRIVATE_SIGNING_KEY_EXPORTED=NO" >> "$OUT/meta/MODULE_SIGNING.txt"
fi

if [ -f "$PRODUCT_OUT/boot.img" ]; then
    cp -f "$PRODUCT_OUT/boot.img" "$OUT/seeds/boot_seed.img"
    echo "BOOT_SEED=present" > "$OUT/meta/SEEDS.txt"
else
    echo "BOOT_SEED=missing" > "$OUT/meta/SEEDS.txt"
    fail "Android 16 boot.img seed missing at $PRODUCT_OUT/boot.img"
fi

if [ -f "$PRODUCT_OUT/recovery.img" ]; then
    cp -f "$PRODUCT_OUT/recovery.img" "$OUT/seeds/recovery_seed.img"
    echo "RECOVERY_SEED=present" >> "$OUT/meta/SEEDS.txt"
else
    echo "RECOVERY_SEED=missing" >> "$OUT/meta/SEEDS.txt"
fi

for SPEC in     "$PRODUCT_OUT/system/build.prop:system_build.prop"     "$PRODUCT_OUT/system/system/build.prop:system_system_build.prop"     "$PRODUCT_OUT/product/build.prop:product_build.prop"     "$PRODUCT_OUT/system/product/build.prop:system_product_build.prop"     "$PRODUCT_OUT/module-info.json:module-info.json"
do
    SRC="${SPEC%%:*}"
    DST="${SPEC#*:}"
    if [ -f "$SRC" ]; then
        cp -f "$SRC" "$OUT/meta/$DST"
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
    echo "EXPECTED_BOOT_HEADER_VERSION=0"
    echo "EXPECTED_BOOT_PAGESIZE=2048"
    echo "EXPECTED_KERNEL_ADDR=0x80008000"
    echo "EXPECTED_RAMDISK_ADDR=0x81000000"
    echo "EXPECTED_TAGS_ADDR=0x80000100"
} > "$OUT/meta/EXPORT.txt"

(
    if cd "$OUT"; then
        find . -type f ! -name SHA256SUMS -print0 |
            sort -z |
            xargs -0 -r sha256sum > SHA256SUMS
    else
        fail "cannot enter export directory for SHA256SUMS"
    fi
)

WIN_DESKTOP="$(
    powershell.exe -NoProfile -Command         '[Environment]::GetFolderPath("Desktop")' 2>/dev/null |
    tr -d '\r'
)"
DESKTOP="$(wslpath -u "$WIN_DESKTOP" 2>/dev/null)"

if [ -z "$DESKTOP" ] || [ ! -d "$DESKTOP" ]; then
    DESKTOP="$HOME/Desktop"
    mkdir -p "$DESKTOP"
fi

ARCHIVE="$DESKTOP/$ZIP_NAME"

python3 - "$OUT" "$ARCHIVE" <<'PY_ZIP'
from pathlib import Path
import sys
import zipfile

src = Path(sys.argv[1])
dst = Path(sys.argv[2])

with zipfile.ZipFile(
    dst, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
) as z:
    for p in sorted(src.rglob("*")):
        if p.is_file():
            z.write(p, p.relative_to(src))

with zipfile.ZipFile(dst, "r") as z:
    bad = z.testzip()
    if bad:
        raise SystemExit("corrupt zip entry: " + bad)

print("ARCHIVE_INTEGRITY=PASS")
PY_ZIP
ZIP_RC=$?

if [ "$ZIP_RC" -ne 0 ]; then
    fail "archive creation/integrity failed rc=$ZIP_RC"
fi

# Run the same seed auditor locally before any optional upload.
if [ "$RC" -eq 0 ]; then
    AUDITOR_TMP="/tmp/TB8504_audit_cloud_seed.py"
    AUDITOR_URL="https://raw.githubusercontent.com/$GITHUB_REPO/$GITHUB_TARGET/tb8504-build/scripts/audit-cloud-seed.py"

    if ! command -v curl >/dev/null 2>&1; then
        fail "curl unavailable; cannot perform mandatory local seed audit"
    elif ! curl -fsSL "$AUDITOR_URL" -o "$AUDITOR_TMP"; then
        fail "cannot download mandatory cloud-seed auditor"
    else
        python3 "$AUDITOR_TMP" "$ARCHIVE" 2>&1 | tee "$OUT/meta/LOCAL_SEED_AUDIT.txt"
        AUDIT_RC=${PIPESTATUS[0]}
        if [ "$AUDIT_RC" -ne 0 ]; then
            fail "mandatory local cloud-seed audit failed rc=$AUDIT_RC"
        else
            echo "LOCAL_CLOUD_SEED_AUDIT=PASS"
        fi
    fi
fi

DRAFT_RELEASE_TAG=""

if [ "$UPLOAD_DRAFT" = "1" ] && [ "$RC" -eq 0 ]; then
    if ! command -v gh >/dev/null 2>&1; then
        warn "gh CLI unavailable; draft release upload skipped"
    elif ! gh auth status >/dev/null 2>&1; then
        warn "gh CLI is not authenticated; draft release upload skipped"
    else
        DRAFT_RELEASE_TAG="tb8504-cloud-seed-$STAMP"
        gh release create "$DRAFT_RELEASE_TAG" "$ARCHIVE#TB8504 cloud seed"             --repo "$GITHUB_REPO"             --target "$GITHUB_TARGET"             --draft             --title "TB8504 Android 16 cloud seed $STAMP"             --notes "Private draft seed for the audited TB8504 cloud build lane. Do not publish."
        GH_RC=$?
        if [ "$GH_RC" -ne 0 ]; then
            fail "draft release upload failed rc=$GH_RC"
        else
            SEED_SHA256="$(sha256sum "$ARCHIVE" | awk '{print $1}')"
            REQUEST_PATH="tb8504-build/requests/live/boot-$STAMP.txt"
            REQUEST_BODY="$(printf 'SEED_RELEASE_TAG=%s\nSEED_ZIP_SHA256=%s\n' "$DRAFT_RELEASE_TAG" "$SEED_SHA256")"
            REQUEST_B64="$(printf '%s' "$REQUEST_BODY" | base64 -w0)"

            gh api --method PUT "repos/$GITHUB_REPO/contents/$REQUEST_PATH" -f message="tb8504: request cloud boot repack $STAMP" -f content="$REQUEST_B64" -f branch="$GITHUB_TARGET" >/dev/null
            REQUEST_RC=$?

            if [ "$REQUEST_RC" -ne 0 ]; then
                fail "boot request commit failed rc=$REQUEST_RC"
            else
                echo "BOOT_REQUEST_PATH=$REQUEST_PATH"
            fi
        fi
    fi
fi

echo
echo "CLOUD_SEED_ARCHIVE=$ARCHIVE"
echo "DRAFT_RELEASE_TAG=${DRAFT_RELEASE_TAG:-NONE}"
echo "FINAL_RC=$RC"
echo "NO_BUILD=YES"
echo "NO_FLASH=YES"
exit "$RC"
