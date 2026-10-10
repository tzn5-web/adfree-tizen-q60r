#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
BASE=/home/dre/tb8504-cloud
PACKAGE=$BASE/package
ROOT=/home/dre/android16-tb8504/lineage-23.2
mkdir -p "$BASE" "$ROOT"
touch "$BASE/build-started"
exec > >(tee -a "$BASE/server-build.log") 2>&1
completed=0
interrupted=0
finish() {
 rc=$?
 trap - EXIT INT TERM
 if [ "$interrupted" -ne 0 ]; then rc=$interrupted; fi
 if [ "$rc" -eq 0 ] && [ "$completed" -ne 1 ]; then rc=1; fi
 python3 "$PACKAGE/cloud_control.py" finish "$rc" "$completed"
 sudo /usr/local/sbin/tb8504-deallocate finish-$rc
 exit "$rc"
}
trap finish EXIT
trap 'interrupted=130; exit 130' INT
trap 'interrupted=143; exit 143' TERM
python3 "$PACKAGE/cloud_control.py" start-run f063d07afd77c2debb19d9c60262a24b7e0a7f98
phase() { python3 "$PACKAGE/cloud_control.py" phase "$1"; }
phase dependencies
sudo apt-get update
sudo env DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l apt-get -o DPkg::Lock::Timeout=600 install -y git-core git-lfs gnupg flex bison build-essential zip curl zlib1g-dev libc6-dev-i386 libncurses5 libncurses5-dev x11proto-core-dev libx11-dev lib32z1-dev libgl1-mesa-dev libxml2-utils xsltproc unzip fontconfig python3 python-is-python3 rsync openssl libssl-dev kmod jq bc cpio lz4 libelf-dev dwarves wget file android-sdk-libsparse-utils
if ! sudo swapon --show=NAME --noheadings | grep -Fxq /swap-tb8504; then
 sudo fallocate -l 64G /swap-tb8504
 sudo chmod 600 /swap-tb8504
 sudo mkswap /swap-tb8504
 sudo swapon /swap-tb8504
fi
if ! grep -Fq '/swap-tb8504 ' /etc/fstab; then
 echo '/swap-tb8504 none swap sw 0 0' | sudo tee -a /etc/fstab
fi
mkdir -p "$BASE/bin"
install -m 755 "$PACKAGE/gh-public" "$BASE/bin/gh"
curl --fail --location --retry 3 https://storage.googleapis.com/git-repo-downloads/repo -o "$BASE/bin/repo"
chmod 755 "$BASE/bin/repo"
export PATH="$BASE/bin:$PATH"
git config --global user.name 'TB8504 cloud build'
git config --global user.email 'tb8504-build@localhost'
git config --global gc.auto 0
git config --global http.version HTTP/1.1
git lfs install
export GIT_TERMINAL_PROMPT=0
if ! grep -Fxq '6c063c882ad4ca9be3759a564ad17ca242adf3b7a729bd0fb967174a948a7bcf' "$BASE/source-restored" 2>/dev/null; then
phase manifest
MANIFEST=$BASE/manifest
mkdir -p "$MANIFEST"
cp "$PACKAGE/cloud-default.xml" "$MANIFEST/default.xml"
git -C "$MANIFEST" init -b cloud-build
git -C "$MANIFEST" add default.xml
if ! git -C "$MANIFEST" diff --cached --quiet; then
 git -C "$MANIFEST" commit -m 'Exact audited Android 16 source revisions'
fi
cd "$ROOT"
repo init -u "$MANIFEST" -b cloud-build --depth=1 --no-clone-bundle --git-lfs
mkdir -p .repo/local_manifests
python3 - "$PACKAGE" "$ROOT" <<'PY'
import io,sys,zipfile,tarfile
from pathlib import Path
p,r=map(Path,sys.argv[1:]); z=zipfile.ZipFile(p/'TB8504_CLOUD_SEED_20261009_114026.zip')
with tarfile.open(fileobj=io.BytesIO(z.read('local_manifests.tar.gz'))) as t:
 for m in t.getmembers():
  if m.isfile():
   target=r/'.repo'/m.name
   if not target.resolve().is_relative_to((r/'.repo/local_manifests').resolve()): raise ValueError('Unsafe manifest member')
   target.parent.mkdir(parents=True,exist_ok=True); target.write_bytes(t.extractfile(m).read()); target.chmod(m.mode)
PY
phase source-sync
synced=false
for attempt in 1 2 3; do
 python3 "$PACKAGE/repair-sync-metadata.py" "$ROOT"
 if repo sync -c -j4 --no-clone-bundle --no-tags --fail-fast; then synced=true; break; fi
 echo "SOURCE_SYNC_RETRY=$attempt"
 sleep 30
done
$synced || exit 1
phase restore
python3 "$PACKAGE/restore-sources.py" "$ROOT" "$PACKAGE"
printf '%s\n' '6c063c882ad4ca9be3759a564ad17ca242adf3b7a729bd0fb967174a948a7bcf' > "$BASE/source-restored"
fi
python3 "$PACKAGE/align-git-report.py" "$ROOT" "$PACKAGE"
phase tooling
if [ ! -d "$BASE/tooling/.git" ]; then
 git clone --single-branch --branch tb8504-android16-build https://github.com/tzn5-web/adfree-tizen-q60r.git "$BASE/tooling"
fi
test "$(git -C "$BASE/tooling" remote get-url origin)" = 'https://github.com/tzn5-web/adfree-tizen-q60r.git'
git -C "$BASE/tooling" fetch origin f063d07afd77c2debb19d9c60262a24b7e0a7f98
git -C "$BASE/tooling" checkout --detach f063d07afd77c2debb19d9c60262a24b7e0a7f98
export TB8504_TOOLING_REF=f063d07afd77c2debb19d9c60262a24b7e0a7f98
phase build
python3 -u "$BASE/tooling/tb8504-build/scripts/tb8504-autopilot.py" --root "$ROOT" --goal rom
phase artifacts
python3 "$PACKAGE/cloud_control.py" collect
completed=1
