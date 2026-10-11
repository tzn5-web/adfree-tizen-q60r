#!/usr/bin/env python3
import json,runpy,tempfile
from pathlib import Path
scripts=Path(__file__).resolve().parent
h=runpy.run_path(str(scripts/'apply-legacy-ota-compat.py'))
before=json.loads((scripts/'ota-board-before.json').read_text())['board'].encode()
after=h['transform'](before)
assert after==before+h['BLOCK'].encode() and h['transform'](after)==after
assert after.count(b'AB_OTA_UPDATER := false')==1
assert after.count(b'BOARD_COPY_BOOT_IMAGE_TO_TARGET_FILES := true')==1
intermediate=before+h['BLOCK'].removesuffix(h['BOOT_COPY_BLOCK']+h['SYSTEM_COPY_BLOCK']).encode()
assert h['transform'](intermediate)==after
assert after.count(b'COPY_IMAGES_FOR_TARGET_FILES_ZIP := true')==1
assert h['transform'](after.removesuffix(h['SYSTEM_COPY_BLOCK'].encode()))==after
for bad in (before+b'# unknown\n',after+b'# unknown\n'):
    try:h['transform'](bad)
    except ValueError:pass
    else:raise AssertionError('Unknown board content accepted')
# The actual helper rejects staged changes, unknown HEAD and full-patch edits.
source=(scripts/'apply-legacy-ota-compat.py').read_text()
assert "git('diff','--cached','--name-only')" in source and 'source mutation reverted' in source
runner=runpy.run_path(str(scripts/'tb8504-autopilot.py'))
assert 'apply-legacy-ota-compat.py' in runner['HELPERS']
# Missing/failed Make policy queries must block before the bacon build.
Auto=runner['Autopilot']
Result=runner['CommandResult']
class Fake(Auto):
    def say(self,message=''):pass
    def android_shell(self,command,log_name):
        assert 'COPY_IMAGES_FOR_TARGET_FILES_ZIP' in command
        return Result(self.query_rc,'',command)
    def build_target(self,target):raise AssertionError('Build started despite invalid OTA image policy')
f=object.__new__(Fake)
f.query_rc=0
f.require_ota_image_copy_policy()
f.query_rc=1
try:f.full_rom()
except runner['StopAutopilot']:pass
else:raise AssertionError('Failed OTA policy query accepted')
print('LEGACY_NON_AB_OTA_SELFTEST=PASS')
