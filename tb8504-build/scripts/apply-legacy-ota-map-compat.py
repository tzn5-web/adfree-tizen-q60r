#!/usr/bin/env python3
"""Exclude only unallocated pseudo extents from non-A/B sparse OTA file maps."""
import argparse,difflib,hashlib,subprocess
from pathlib import Path
HEAD='e5aaa62172df0f321e68133fa30f42316376bfe8'
FILE='core/Makefile'
PRE_FILE_SHA='fec7d1d596af088e3c75e6eff5ee355ee3a62bd09565be377fbc24a4030d15d0'
OLD_FILE_SHA='348224a00e7c5f64d21b8622cc91044764d3c7a23bc9231aa41b0f28c2644fa7'
POST_FILE_SHA='750ea2ffdc3a89c3858f52cba7099856695b058f496d8d9ebb023567608198fb'
POST_PATCH_SHA='8aef916bf9894963d340f89dd945bd26dec783e12ef5d3c509222bcbf96d2f99'
ORIGINAL='$(if $(3),$(HOST_OUT_EXECUTABLES)/map_file_generator $(1) $(2)/$(3).map))'
REPLACEMENT='$(if $(3),$(HOST_OUT_EXECUTABLES)/map_file_generator $(1) $(2)/$(3).map$(if $(filter false,$(AB_OTA_UPDATER)), --legacy_non_ab)))'
digest=lambda data:hashlib.sha256(data).hexdigest()

def transform(text):
    if text.count(ORIGINAL)!=1 or REPLACEMENT in text:
        raise ValueError('Unexpected target-files image-copy recipe')
    return text.replace(ORIGINAL,REPLACEMENT)

def apply(repo):
    def git(*args):return subprocess.check_output(['git',*args],cwd=repo)
    if git('rev-parse','HEAD').decode().strip()!=HEAD:raise ValueError('Unapproved build/make HEAD')
    if git('ls-files','--others','--exclude-standard').strip():raise ValueError('Untracked build/make sources')
    if git('diff','--cached','--name-only').strip():raise ValueError('Staged build/make changes')
    if not set(git('diff','--name-only','HEAD').decode().splitlines()).issubset({FILE}):raise ValueError('Unrelated build/make changes')
    path=repo/FILE
    if path.is_symlink() or not path.is_file():raise ValueError('Unsafe Makefile source path')
    original=git('show','HEAD:'+FILE)
    if digest(original)!=PRE_FILE_SHA:raise ValueError('Makefile baseline mismatch')
    desired=transform(original.decode()).encode()
    if digest(desired)!=POST_FILE_SHA:raise ValueError('Makefile reviewed postimage mismatch')
    current=path.read_bytes()
    if digest(current) not in (PRE_FILE_SHA,OLD_FILE_SHA,POST_FILE_SHA):raise ValueError('Unreviewed Makefile content')
    changed=current!=desired
    subprocess.run(['git','config','--local','core.abbrev','40'],cwd=repo,check=True)
    if changed:path.write_bytes(desired)
    if digest(git('diff','--binary','HEAD'))!=POST_PATCH_SHA:
        if changed:path.write_bytes(current)
        raise ValueError('Full build/make patch mismatch')
    patch=''.join(difflib.unified_diff(current.decode().splitlines(True),desired.decode().splitlines(True),fromfile='a/'+FILE,tofile='b/'+FILE))
    return changed,patch,POST_PATCH_SHA

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--build-make',type=Path,required=True);ap.add_argument('--patch-out',type=Path,required=True);ap.add_argument('--report-out',type=Path,required=True);args=ap.parse_args()
    try:changed,patch,sha=apply(args.build_make)
    except (ValueError,subprocess.CalledProcessError) as exc:raise SystemExit('LEGACY_OTA_MAP_COMPAT_FAIL='+str(exc))
    args.patch_out.write_text(patch)
    report='LEGACY_OTA_MAP_COMPAT_STATE='+('APPLIED' if changed else 'ALREADY_APPLIED')+f'\nCHANGED_FILES={int(changed)}\nBUILD_MAKE_PATCH_SHA256={sha}\nLEGACY_OTA_MAP_COMPAT=PASS\n'
    args.report_out.write_text(report);print(report,end='')

if __name__=='__main__':main()
