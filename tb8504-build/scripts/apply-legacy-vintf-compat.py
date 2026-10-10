#!/usr/bin/env python3
"""Preserve legacy HAL support while using frozen Android16 release interfaces."""
import argparse,difflib,hashlib,subprocess
from pathlib import Path
HEAD = '245eadab3c1e1732da1e4c1c99b2cc2fed74b91b'
PRE_PATCH_SHA = 'a27d2e36b843d63eb29d99995d06701ecc80b8738f41ad80e9903032913710f6'
POST_PATCH_SHA = '9993f390b5a0cd2892f10f02d09e55f47a4e07912b94946f8dc312121fb83002'
PRE_FILE_SHA = '409d8db866b0724453a954d05ad6ee100fd462b1201374b650ba65a2f959b654'
POST_FILE_SHA = 'd6e3edd33a6bc22f07be2807b8fa41b0b56c7990c686fc989621d8b5a837eadb'
BLOCK = '    <!-- Bluetooth audio HIDL 2.0 is still supported by the pinned platform client. -->\n    <hal format="hidl" optional="true">\n        <name>android.hardware.bluetooth.audio</name>\n        <version>2.0</version>\n        <interface>\n            <name>IBluetoothAudioProvidersFactory</name>\n            <instance>default</instance>\n        </interface>\n    </hal>\n\n'

def digest(data):return hashlib.sha256(data).hexdigest()
def transform(data):
    if digest(data)==POST_FILE_SHA:return data
    if digest(data)!=PRE_FILE_SHA:raise ValueError('Unreviewed framework matrix')
    text=data.decode()
    for version,replacement,name in ((2,'1-2','android.hardware.drm'),(4,'3-4','android.hardware.wifi'),(5,'4-5','android.hardware.wifi.supplicant')):
        anchor=f'<name>{name}</name>\n        <version>{version}</version>'
        if text.count(anchor)!=1:raise ValueError('Unexpected AIDL matrix entry')
        text=text.replace(anchor,anchor.replace(f'<version>{version}</version>',f'<version>{replacement}</version>'))
    if text.count('</compatibility-matrix>')!=1:raise ValueError('Ambiguous matrix')
    result=text.replace('</compatibility-matrix>',BLOCK+'</compatibility-matrix>').encode()
    if digest(result)!=POST_FILE_SHA:raise ValueError('VINTF matrix postimage mismatch')
    return result

def apply(device):
    def git(*args):return subprocess.check_output(['git',*args],cwd=device)
    if git('rev-parse','HEAD').decode().strip()!=HEAD:raise ValueError('Device HEAD differs from approved source')
    if git('diff','--cached','--name-only').strip():raise ValueError('Unreviewed staged source changes')
    full=digest(git('diff','--binary','HEAD'))
    if full not in (PRE_PATCH_SHA,POST_PATCH_SHA):raise ValueError('Unreviewed full device patch')
    path=device/'framework_compatibility_matrix.xml'
    if path.is_symlink() or not path.is_file():raise ValueError('Framework matrix is not a regular source file')
    original=path.read_bytes();desired=transform(original);changed=int(original!=desired)
    if changed:
        if full!=PRE_PATCH_SHA:raise ValueError('Preimage contract mismatch')
        path.write_bytes(desired)
    actual=digest(git('diff','--binary','HEAD'))
    if actual!=POST_PATCH_SHA:
        if changed:path.write_bytes(original)
        raise ValueError('Full device postimage mismatch; source mutation reverted')
    diff=''.join(difflib.unified_diff(original.decode().splitlines(True),desired.decode().splitlines(True),fromfile='a/framework_compatibility_matrix.xml',tofile='b/framework_compatibility_matrix.xml'))
    return changed,diff,actual

def main():
    parser=argparse.ArgumentParser()
    for name in ('device','patch-out','report-out'):parser.add_argument('--'+name,required=True,type=Path)
    args=parser.parse_args()
    try:changed,patch,sha=apply(args.device)
    except (ValueError,subprocess.CalledProcessError) as exc:raise SystemExit('LEGACY_VINTF_COMPAT_FAIL='+str(exc))
    args.patch_out.parent.mkdir(parents=True,exist_ok=True);args.patch_out.write_text(patch)
    report=('LEGACY_VINTF_COMPAT_STATE='+('APPLIED' if changed else 'ALREADY_APPLIED')+'\n'+f'CHANGED_FILES={changed}\nVINTF_DEVICE_PATCH_SHA256={sha}\nLEGACY_VINTF_COMPAT=PASS\n')
    args.report_out.write_text(report);print(report,end='')
if __name__=='__main__':main()
