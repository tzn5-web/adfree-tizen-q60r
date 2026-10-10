#!/usr/bin/env python3
"""Validate exact radio source transform and GNU Make package selection."""
import json,runpy,subprocess,tempfile
from pathlib import Path
here=Path(__file__).resolve().parent
mod=runpy.run_path(str(here/'apply-radio-runtime-compat.py'))
before=json.loads((here/'radio-runtime-device-before.json').read_text())['device_mk'].encode()
after=mod['transform'](before)
assert mod['transform'](after)==after
assert before==after[:-len(mod['BLOCK'].encode())]
for bad in [before+b'# unreviewed\n',before.replace(b'# RIL',b'# altered RIL'),after+b'# unreviewed\n']:
    try:mod['transform'](bad)
    except ValueError:pass
    else:raise AssertionError('Unreviewed source accepted')
with tempfile.TemporaryDirectory() as temp:
    path=Path(temp)/'fixture.mk'
    path.write_text('PRODUCT_PACKAGES := existing_radio_service\n'+mod['BLOCK']+'\nall:\n\t@echo $(PRODUCT_PACKAGES)\n')
    packages=subprocess.check_output(['make','--no-print-directory','-f',str(path)],text=True).split()
    assert packages==['existing_radio_service',*mod['PACKAGES']]
    assert all(p.endswith('.vendor:64') for p in packages[1:])
    result=subprocess.run(['python3',str(here/'apply-radio-runtime-compat.py'),'--device',temp,'--patch-out',str(Path(temp)/'out.patch'),'--report-out',str(Path(temp)/'out.txt')],capture_output=True,text=True)
    assert result.returncode!=0 and not (Path(temp)/'out.patch').exists()
print('RADIO_RUNTIME_COMPAT_SELFTEST=PASS')
