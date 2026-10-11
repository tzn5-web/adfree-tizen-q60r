#!/usr/bin/env python3
import json,runpy,tempfile
from pathlib import Path
scripts=Path(__file__).resolve().parent
h=runpy.run_path(str(scripts/'apply-legacy-ota-compat.py'))
before=json.loads((scripts/'ota-board-before.json').read_text())['board'].encode()
after=h['transform'](before)
assert after==before+h['BLOCK'].encode() and h['transform'](after)==after
assert after.count(b'AB_OTA_UPDATER := false')==1
for bad in (before+b'# unknown\n',after+b'# unknown\n'):
    try:h['transform'](bad)
    except ValueError:pass
    else:raise AssertionError('Unknown board content accepted')
# The actual helper rejects staged changes, unknown HEAD and full-patch edits.
source=(scripts/'apply-legacy-ota-compat.py').read_text()
assert "git('diff','--cached','--name-only')" in source and 'source mutation reverted' in source
runner=runpy.run_path(str(scripts/'tb8504-autopilot.py'))
assert 'apply-legacy-ota-compat.py' in runner['HELPERS']
print('LEGACY_NON_AB_OTA_SELFTEST=PASS')
