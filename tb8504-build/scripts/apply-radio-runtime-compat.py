#!/usr/bin/env python3
"""Install only the five audited missing 64-bit legacy radio HIDL providers."""
import argparse,difflib,hashlib,subprocess
from pathlib import Path

HEAD = '245eadab3c1e1732da1e4c1c99b2cc2fed74b91b'
PRE_PATCH_SHA = '3d29fd98d76c4512a33cf0ee9042777a7018374de1c487686812267e95e475b6'
FINAL_VINTF_PATCH_SHA = '9993f390b5a0cd2892f10f02d09e55f47a4e07912b94946f8dc312121fb83002'
INTERMEDIATE_OTA_PATCH_SHA = '76eeb76ac096c58cf79594d1270930d575697353e593ca423f7de709324ab21d'
BOOT_PAIR_OTA_PATCH_SHA = '686cc8e2aee07821d72ae8b631fe2ed97ee118e8903b4204547062bd232a6f18'
SYSTEM_PAIR_OTA_PATCH_SHA = 'ca2708abb97be0aec732621b83ea89dd362392b1771c8ba03849e71136ac43a8'
FINAL_OTA_PATCH_SHA = 'd1a8b818ff535af0e7bf42c3e041bde9ddeffce90a2de46a5361809bc655401c'
POST_PATCH_SHA = 'a27d2e36b843d63eb29d99995d06701ecc80b8738f41ad80e9903032913710f6'
PRE_FILE_SHA = 'c3b9d9c2bfbec91c93e5374a5bfbf23d1cedf85d4b6bc20c86ecf3123a2146a4'
POST_FILE_SHA = 'aed1d31f110c5823b3002c8371d2edd5f31b33e33320d12e64925ec11b8e6670'
PACKAGES = ('android.hardware.secure_element@1.0.vendor:64', 'android.hardware.radio.config@1.0.vendor:64', 'android.hardware.radio@1.2.vendor:64', 'android.hardware.radio@1.3.vendor:64', 'android.hardware.radio@1.4.vendor:64')
BLOCK = '\n# Android 16 legacy radio vendor ELF dependencies\nPRODUCT_PACKAGES += \\\n    android.hardware.secure_element@1.0.vendor:64 \\\n    android.hardware.radio.config@1.0.vendor:64 \\\n    android.hardware.radio@1.2.vendor:64 \\\n    android.hardware.radio@1.3.vendor:64 \\\n    android.hardware.radio@1.4.vendor:64\n'

def digest(data):return hashlib.sha256(data).hexdigest()
def transform(data):
    if digest(data)==POST_FILE_SHA:return data
    if digest(data)!=PRE_FILE_SHA:raise ValueError('Unreviewed device.mk content')
    result=data+BLOCK.encode()
    if digest(result)!=POST_FILE_SHA:raise ValueError('Radio transform postimage mismatch')
    return result

def apply(device):
    def git(*args):return subprocess.check_output(['git',*args],cwd=device)
    if git('rev-parse','HEAD').decode().strip()!=HEAD:raise ValueError('Device HEAD differs from approved source')
    if git('diff','--cached','--name-only').strip():raise ValueError('Staged device changes are not approved')
    patch_sha=digest(git('diff','--binary','HEAD'))
    if patch_sha not in (PRE_PATCH_SHA,POST_PATCH_SHA,FINAL_VINTF_PATCH_SHA,INTERMEDIATE_OTA_PATCH_SHA,BOOT_PAIR_OTA_PATCH_SHA,SYSTEM_PAIR_OTA_PATCH_SHA,FINAL_OTA_PATCH_SHA):raise ValueError('Full device source patch is unreviewed')
    path=device/'device.mk'
    if path.is_symlink() or not path.is_file():raise ValueError('device.mk is not a regular source file')
    current=path.read_bytes();desired=transform(current)
    changed=int(current!=desired)
    if changed:
        if patch_sha!=PRE_PATCH_SHA:raise ValueError('Device preimage contract mismatch')
        path.write_bytes(desired)
    actual=digest(git('diff','--binary','HEAD'))
    if actual not in (POST_PATCH_SHA,FINAL_VINTF_PATCH_SHA,INTERMEDIATE_OTA_PATCH_SHA,BOOT_PAIR_OTA_PATCH_SHA,SYSTEM_PAIR_OTA_PATCH_SHA,FINAL_OTA_PATCH_SHA) or (actual in (FINAL_VINTF_PATCH_SHA,INTERMEDIATE_OTA_PATCH_SHA,BOOT_PAIR_OTA_PATCH_SHA,SYSTEM_PAIR_OTA_PATCH_SHA,FINAL_OTA_PATCH_SHA) and (changed or patch_sha!=actual)):
        if changed:path.write_bytes(current)
        raise ValueError('Full device postimage mismatch; source mutation reverted')
    patch=''.join(difflib.unified_diff(current.decode().splitlines(True),desired.decode().splitlines(True),fromfile='a/device.mk',tofile='b/device.mk'))
    return changed,patch,actual

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--device',required=True,type=Path)
    parser.add_argument('--patch-out',required=True,type=Path)
    parser.add_argument('--report-out',required=True,type=Path)
    args=parser.parse_args()
    try:changed,patch,sha=apply(args.device)
    except (ValueError,subprocess.CalledProcessError) as exc:raise SystemExit('RADIO_RUNTIME_COMPAT_FAIL='+str(exc))
    args.patch_out.parent.mkdir(parents=True,exist_ok=True);args.patch_out.write_text(patch)
    report=('RADIO_RUNTIME_COMPAT_STATE='+('APPLIED' if changed else 'ALREADY_APPLIED')+'\n'+f'CHANGED_FILES={changed}\nRADIO_DEVICE_PATCH_SHA256={sha}\nRADIO_RUNTIME_COMPAT=PASS\n')
    args.report_out.parent.mkdir(parents=True,exist_ok=True);args.report_out.write_text(report);print(report,end='')
if __name__=='__main__':main()
