#!/usr/bin/env python3
"""Select recovery-based OTA for the exact legacy single-slot TB8504 sources."""
import argparse,difflib,hashlib,subprocess
from pathlib import Path
HEAD = '245eadab3c1e1732da1e4c1c99b2cc2fed74b91b'
PRE_PATCH_SHA = '9993f390b5a0cd2892f10f02d09e55f47a4e07912b94946f8dc312121fb83002'
INTERMEDIATE_PATCH_SHA = '76eeb76ac096c58cf79594d1270930d575697353e593ca423f7de709324ab21d'
BOOT_PAIR_PATCH_SHA = '686cc8e2aee07821d72ae8b631fe2ed97ee118e8903b4204547062bd232a6f18'
SYSTEM_PAIR_PATCH_SHA = 'ca2708abb97be0aec732621b83ea89dd362392b1771c8ba03849e71136ac43a8'
POST_PATCH_SHA = 'd1a8b818ff535af0e7bf42c3e041bde9ddeffce90a2de46a5361809bc655401c'
PRE_FILE_SHA = '25dd37628eb7dddd5b68c99a9521b02a46e6b21c086ab42fddb8d65787605bfb'
INTERMEDIATE_FILE_SHA = '00307fe827d9a35230c0a9d7cde11f8466c9a6ca21b8502756f7d8fceaf1f2c0'
BOOT_PAIR_FILE_SHA = '32fb00a7c453ee4ee3d24da29682c44a45191586413d835c6dd7fd618e937d97'
SYSTEM_PAIR_FILE_SHA = 'c675763308f8373b646f6be7708acd2812ff154c5f2497b0148dbd0f8057ca7e'
POST_FILE_SHA = '62dad82312be417d48be9c851e4acf13d4cc41529943d2cf6ce17c8a428e08db'
BLOCK = '\n# TB8504 has one boot/system slot and a dedicated recovery partition.\n# Android 16 defaults an unspecified updater to A/B; select the real topology.\nAB_OTA_UPDATER := false\n\n# Preserve the audited first-stage boot image and its device nodes in the OTA.\nBOARD_COPY_BOOT_IMAGE_TO_TARGET_FILES := true\n\n# Keep OTA partition blocks identical to the separately audited images.\nCOPY_IMAGES_FOR_TARGET_FILES_ZIP := true\n\n# The target-files copy recipe requires an explicit supported filesystem type.\nBOARD_SYSTEMIMAGE_FILE_SYSTEM_TYPE := ext4\n'
SYSTEM_FS_BLOCK = '\n# The target-files copy recipe requires an explicit supported filesystem type.\nBOARD_SYSTEMIMAGE_FILE_SYSTEM_TYPE := ext4\n'
SYSTEM_COPY_BLOCK = '\n# Keep OTA partition blocks identical to the separately audited images.\nCOPY_IMAGES_FOR_TARGET_FILES_ZIP := true\n'
BOOT_COPY_BLOCK = '\n# Preserve the audited first-stage boot image and its device nodes in the OTA.\nBOARD_COPY_BOOT_IMAGE_TO_TARGET_FILES := true\n'

def digest(data):return hashlib.sha256(data).hexdigest()
def transform(data):
    if digest(data)==POST_FILE_SHA:return data
    if digest(data)==SYSTEM_PAIR_FILE_SHA:
        result=data+SYSTEM_FS_BLOCK.encode()
    elif digest(data)==BOOT_PAIR_FILE_SHA:
        result=data+SYSTEM_COPY_BLOCK.encode()+SYSTEM_FS_BLOCK.encode()
    elif digest(data)==INTERMEDIATE_FILE_SHA:
        result=data+BOOT_COPY_BLOCK.encode()+SYSTEM_COPY_BLOCK.encode()+SYSTEM_FS_BLOCK.encode()
    elif digest(data)==PRE_FILE_SHA:
        result=data+BLOCK.encode()
    else:raise ValueError('Unreviewed BoardConfig content')
    if digest(result)!=POST_FILE_SHA:raise ValueError('OTA BoardConfig postimage mismatch')
    return result

def apply(device):
    def git(*args):return subprocess.check_output(['git',*args],cwd=device)
    if git('rev-parse','HEAD').decode().strip()!=HEAD:raise ValueError('Device HEAD differs from approved source')
    if git('diff','--cached','--name-only').strip():raise ValueError('Unreviewed staged source changes')
    full=digest(git('diff','--binary','HEAD'))
    if full not in (PRE_PATCH_SHA,INTERMEDIATE_PATCH_SHA,BOOT_PAIR_PATCH_SHA,SYSTEM_PAIR_PATCH_SHA,POST_PATCH_SHA):raise ValueError('Unreviewed full device patch')
    path=device/'BoardConfig.mk'
    if path.is_symlink() or not path.is_file():raise ValueError('BoardConfig is not a regular source file')
    original=path.read_bytes();desired=transform(original);changed=int(original!=desired)
    if changed:
        if full not in (PRE_PATCH_SHA,INTERMEDIATE_PATCH_SHA,BOOT_PAIR_PATCH_SHA,SYSTEM_PAIR_PATCH_SHA):raise ValueError('Preimage contract mismatch')
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
