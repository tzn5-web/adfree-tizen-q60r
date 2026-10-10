#!/usr/bin/env python3
"""Check exact VINTF source bytes and fail-closed build-policy applicability."""
import contextlib,hashlib,io,json,runpy,tempfile,xml.etree.ElementTree as ET
from pathlib import Path
here=Path(__file__).resolve().parent
helper=runpy.run_path(str(here/'apply-legacy-vintf-compat.py'))
before=json.loads((here/'vintf-device-matrix-before.json').read_text())['matrix'].encode()
after=helper['transform'](before)
assert helper['transform'](after)==after
for bad in (before+b'<!-- unreviewed -->', after+b'<!-- unreviewed -->'):
    try:helper['transform'](bad)
    except ValueError:pass
    else:raise AssertionError('Unreviewed matrix accepted')
tree=ET.fromstring(after);hals={h.findtext('name'):h for h in tree.findall('hal')}
assert hals['android.hardware.bluetooth.audio'].findtext('version')=='2.0'
assert hals['android.hardware.bluetooth.audio'].findtext('interface/name')=='IBluetoothAudioProvidersFactory'
assert hals['android.hardware.wifi'].findtext('version')=='3-4'
assert hals['android.hardware.wifi.supplicant'].findtext('version')=='4-5'
assert hals['android.hardware.drm'].findtext('version')=='1-2'
assert set(hals)=={h.findtext('name') for h in ET.fromstring(before).findall('hal')}|{'android.hardware.bluetooth.audio'}
policy=runpy.run_path(str(here/'audit-built-output.py'))['read_vintf_kernel_policy']
valid={'TARGET_RELEASE':'bp4a','PLATFORM_SDK_VERSION':'36','RELEASE_AIDL_USE_UNFROZEN':'','PRODUCT_SHIPPING_API_LEVEL':'25','PRODUCT_OTA_ENFORCE_VINTF_KERNEL_REQUIREMENTS':''}
with tempfile.TemporaryDirectory() as temp:
    p=Path(temp)/'release.txt'
    def check(values,api='25'):
        p.write_text(''.join(f'{k}={v}\n' for k,v in values.items()))
        with contextlib.redirect_stdout(io.StringIO()):return policy(p,api)
    assert check(valid) is False
    assert check(dict(valid,PRODUCT_OTA_ENFORCE_VINTF_KERNEL_REQUIREMENTS='false')) is False
    assert check(dict(valid,PRODUCT_OTA_ENFORCE_VINTF_KERNEL_REQUIREMENTS='true')) is True
    bads=[dict(valid,TARGET_RELEASE='trunk_staging'),dict(valid,RELEASE_AIDL_USE_UNFROZEN='true'),dict(valid,PRODUCT_SHIPPING_API_LEVEL='24'),dict(valid,PRODUCT_OTA_ENFORCE_VINTF_KERNEL_REQUIREMENTS='unknown')]
    for missing in ('PRODUCT_OTA_ENFORCE_VINTF_KERNEL_REQUIREMENTS','RELEASE_AIDL_USE_UNFROZEN','PRODUCT_SHIPPING_API_LEVEL'):
        bad=dict(valid);del bad[missing];bads.append(bad)
    for bad in bads:
        try:check(bad)
        except SystemExit as exc:assert exc.code==2
        else:raise AssertionError('Unknown/mismatched build policy accepted')
    try:check(dict(valid,PRODUCT_SHIPPING_API_LEVEL='29'),'29')
    except SystemExit as exc:assert exc.code==2
    else:raise AssertionError('Legacy exception applied to API29')
    p.write_text(''.join(f'{k}={v}\n' for k,v in valid.items())+'TARGET_RELEASE=bp4a\n')
    with contextlib.redirect_stdout(io.StringIO()):
        try:policy(p,'25')
        except SystemExit as exc:assert exc.code==2
        else:raise AssertionError('Duplicate policy accepted')
print('LEGACY_VINTF_AND_BUILD_POLICY_SELFTEST=PASS')
