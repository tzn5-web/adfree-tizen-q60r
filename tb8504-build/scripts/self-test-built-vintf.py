#!/usr/bin/env python3
"""Exercise built VINTF deployment semantics without Android compilation."""
import contextlib,io,runpy,tempfile
from pathlib import Path
here=Path(__file__).resolve().parent
audit=runpy.run_path(str(here/'audit-built-output.py'))['audit_vintf']
ims='<manifest version="9.0" type="device"><hal><name>vendor.qti.imsrtpservice</name></hal></manifest>'
wfd='<hal FORMAT><name>com.qualcomm.qti.wifidisplayhal</name></hal>'
cases=[
    ('<compatibility-matrix version="9.0" type="framework">'+wfd.replace('FORMAT','')+'</compatibility-matrix>',True),
    ('<compatibility-matrix version="8.0" type="framework">'+wfd.replace('FORMAT','optional="true"')+'</compatibility-matrix>',True),
    ('<compatibility-matrix version="8.0" type="framework">'+wfd.replace('FORMAT','')+'</compatibility-matrix>',False),
    ('<manifest version="9.0" type="device">'+wfd.replace('FORMAT','optional="true"')+'</manifest>',False),
    ('<compatibility-matrix version="999.0" type="framework">'+wfd.replace('FORMAT','')+'</compatibility-matrix>',False),
    ('<manifest version="9.0" type="device"><!-- com.qualcomm.qti.wifidisplayhal --></manifest>',True),
    ('<manifest><hal>',False),
]
with tempfile.TemporaryDirectory() as temp:
    out=Path(temp);vintf=out/'system/vendor/etc/vintf';vintf.mkdir(parents=True)
    (vintf/'ims.xml').write_text(ims)
    for xml,wanted in cases:
        (vintf/'case.xml').write_text(xml)
        with contextlib.redirect_stdout(io.StringIO()):
            try:audit(out)
            except SystemExit as exc:
                assert exc.code==2;accepted=False
            else:accepted=True
        assert accepted==wanted,(xml,accepted,wanted)
    (vintf/'case.xml').unlink()
    (vintf/'ims.xml').write_text(ims.replace('manifest','compatibility-matrix'))
    with contextlib.redirect_stdout(io.StringIO()):
        try:audit(out)
        except SystemExit:pass
        else:raise AssertionError('Matrix-only IMS reference accepted as deployed manifest HAL')
print('BUILT_VINTF_SEMANTICS_SELFTEST=PASS')

resolve=runpy.run_path(str(here/'audit-built-output.py'))['vintf_partition_mappings']
with tempfile.TemporaryDirectory() as temp:
    out=Path(temp)
    for rel in ('system/vendor/odm','system/product','system/system_ext','apex'):
        (out/rel).mkdir(parents=True)
    mapped=dict(resolve(out))
    assert mapped['/vendor']==out/'system/vendor'
    assert mapped['/odm']==out/'system/vendor/odm'
    assert mapped['/product']==out/'system/product'
    assert mapped['/system_ext']==out/'system/system_ext'
    for rel in ('vendor/odm','product','system_ext'):(out/rel).mkdir(parents=True)
    mapped=dict(resolve(out))
    assert mapped['/vendor']==out/'vendor' and mapped['/odm']==out/'vendor/odm'
    assert mapped['/product']==out/'product' and mapped['/system_ext']==out/'system_ext'
    (out/'apex').rmdir()
    with contextlib.redirect_stdout(io.StringIO()):
        try:resolve(out)
        except SystemExit as exc:assert exc.code==2
        else:raise AssertionError('Missing VINTF root accepted')
print('BUILT_VINTF_PARTITION_MAPPING_SELFTEST=PASS')
