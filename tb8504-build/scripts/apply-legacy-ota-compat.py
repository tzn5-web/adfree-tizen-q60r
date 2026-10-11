#!/usr/bin/env python3
"""Select recovery-based OTA for the exact legacy single-slot TB8504 sources."""
import argparse,difflib,hashlib,subprocess
from pathlib import Path
HEAD = '245eadab3c1e1732da1e4c1c99b2cc2fed74b91b'
PRE_PATCH_SHA = '9993f390b5a0cd2892f10f02d09e55f47a4e07912b94946f8dc312121fb83002'
POST_PATCH_SHA = '76eeb76ac096c58cf79594d1270930d575697353e593ca423f7de709324ab21d'
PRE_FILE_SHA = '25dd37628eb7dddd5b68c99a9521b02a46e6b21c086ab42fddb8d65787605bfb'
POST_FILE_SHA = '00307fe827d9a35230c0a9d7cde11f8466c9a6ca21b8502756f7d8fceaf1f2c0'
BLOCK = '\n# TB8504 has one boot/system slot and a dedicated recovery partition.\n# Android 16 defaults an unspecified updater to A/B; select the real topology.\nAB_OTA_UPDATER := false\n'

def digest(data):return hashlib.sha256(data).hexdigest()
def transform(data):
    if digest(data)==POST_FILE_SHA:return data
    if digest(data)!=PRE_FILE_SHA:raise ValueError('Unreviewed BoardConfig content')
    result=data+BLOCK.encode()
    if digest(result)!=POST_FILE_SHA:raise ValueError('OTA BoardConfig postimage mismatch')
    return result

def apply(device):
    def git(*args):return subprocess.check_output(['git',*args],cwd=device)
    if git('rev-parse','HEAD').decode().strip()!=HEAD:raise ValueError('Device HEAD differs from approved source')
    if git('diff','--cached','--name-only').strip():raise ValueError('Unreviewed staged source changes')
    full=digest(git('diff','--binary','HEAD'))
    if full not in (PRE_PATCH_SHA,POST_PATCH_SHA):raise ValueError('Unreviewed full device patch')
    path=device/'BoardConfig.mk'
    if path.is_symlink() or not path.is_file():raise ValueError('BoardConfig is not a regular source file')
    original=path.read_bytes();desired=transform(original);changed=int(original!=desired)
    if changed:
        if full!=PRE_PATCH_SHA:raise ValueError('Preimage contract mismatch')
        path.write_bytes(desired)
    actual=digest(git('diff','--binary','HEAD'))
    if actual!=POST_PATCH_SHA:
        if changed:path.write_bytes(original)
        raise ValueError('Full device postimage mismatch; source mutation reverted')
    diff=''.join(difflib.unified_diff(original.decode().splitlines(True),desired.decode().splitlines(True),fromfile='a/BoardConfig.mk',tofile='b/BoardConfig.mk'))
    return changed,diff,actual

def main():
    parser=argparse.ArgumentParser()
    for name in ('device','patch-out','report-out'):parser.add_argument('--'+name,required=True,type=Path)
    args=parser.parse_args()
    try:changed,patch,sha=apply(args.device)
    except (ValueError,subprocess.CalledProcessError) as exc:raise SystemExit('LEGACY_OTA_COMPAT_FAIL='+str(exc))
    args.patch_out.parent.mkdir(parents=True,exist_ok=True);args.patch_out.write_text(patch)
    report=('LEGACY_OTA_COMPAT_STATE='+('APPLIED' if changed else 'ALREADY_APPLIED')+'\n'+f'CHANGED_FILES={changed}\nOTA_DEVICE_PATCH_SHA256={sha}\nLEGACY_OTA_COMPAT=PASS\n')
    args.report_out.write_text(report);print(report,end='')
if __name__=='__main__':main()
