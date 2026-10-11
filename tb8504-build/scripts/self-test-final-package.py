#!/usr/bin/env python3
"""Regression tests for real Lenovo fingerprints and fail-closed OTA identity."""
import contextlib
import io
import runpy
from pathlib import Path

h=runpy.run_path(str(Path(__file__).with_name('audit-final-package.py')))
check=h['audit_ota_identity']
stock='Lenovo/TB-8504X/TB-8504X:8.1.0/OPM1.171019.019/8504X_S001031_191204_ROW:user/release-keys'
meta={'pre-device':'TB-8504X,TB-8504F,tb-8504x,tb-8504f,tb_8504',
      'post-build':stock,'post-sdk-level':'36','post-build-incremental':'1791700250'}
props={'ro.build.fingerprint':stock,'ro.build.version.release':'16',
       'ro.build.version.sdk':'36','ro.build.version.incremental':'1791700250'}
assert 'TB-8504X' in check(meta,props)
for model in ('TB-8504F','TB8504'):
    fp=stock.replace('TB-8504X',model)
    check({**meta,'post-build':fp},{**props,'ro.build.fingerprint':fp})
bad_cases=[
    ({'pre-device':'other'},{}),
    ({'post-build':stock.replace('TB-8504X','other')},{}),
    ({'post-build':stock.replace('TB-8504X','otherTB8504other')},{}),
    ({'post-build':'TB8504'},{}),
    ({'post-build':stock.replace('/TB-8504X:', '/other:')},{}),
    ({'post-sdk-level':'35'},{}),
    ({'post-sdk-level':''},{}),
    ({},{'ro.build.version.release':'8.1.0'}),
    ({},{'ro.build.version.sdk':'35'}),
    ({},{'ro.build.fingerprint':stock+'other'}),
    ({'post-build-incremental':'old'},{}),
    ({'post-build-incremental':''},{}),
]
for overrides,output in bad_cases:
    with contextlib.redirect_stdout(io.StringIO()):
        try:check({**meta,**overrides},{**props,**output})
        except SystemExit as exc:assert exc.code==2
        else:raise AssertionError((overrides,output))
print('FINAL_PACKAGE_IDENTITY_SELFTEST=PASS')
