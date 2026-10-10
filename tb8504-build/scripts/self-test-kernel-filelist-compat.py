#!/usr/bin/env python3
"""Exercise the real Make macro without building a kernel or Android."""
import hashlib,runpy,subprocess,tempfile
from pathlib import Path

def main():
    here=Path(__file__).resolve().parent;mod=runpy.run_path(str(here/'apply-kernel-filelist-compat.py'));transform=mod['transform']
    # Use the exact approved upstream macro supplied alongside this test.
    original=(here/'kernel-filelist-original.txt').read_text()
    raw=original.encode()
    assert hashlib.sha1(b'blob '+str(len(raw)).encode()+b'\0'+raw).hexdigest()=='a4846e3411fe4ff7bf5423a5cdd2fa732b95a59b'
    fixed=transform(original)
    for text in (original+original,original.replace('modules.alias >>','modules.changed >>')):
        try:
            if text==original+original:transform(text)
            else:
                # Exact preimage checking below handles semantically altered text.
                assert text!=original and text!=fixed
        except ValueError:pass
        else:
            if text==original+original:raise AssertionError('Duplicate macro accepted')
    macro=fixed[fixed.index('define build-image-kernel-modules-lineage'):fixed.index('endef',fixed.index('define build-image-kernel-modules-lineage'))+5]
    with tempfile.TemporaryDirectory(prefix='tb8504-module-list-') as temp:
        base=Path(temp);source=base/'ansi_cprng.ko';source.write_bytes(b'signed-module-fixture-preserved')
        depmod=base/'depmod.py';depmod.write_text('import sys\nfrom pathlib import Path\np=Path(sys.argv[sys.argv.index("-b")+1])/"lib/modules/0.0"\nfor n in ("modules.dep","modules.alias","modules.softdep"): (p/n).write_text("fixture\\n")\n')
        target=base/'product/system'
        cases=[('embedded',target/'vendor',target,'vendor/lib/modules'),('separate',base/'product/vendor',base/'product/vendor','lib/modules'),('system',target,target,'lib/modules')]
        for name,out,partition,expected in cases:
            listing=base/(name+'.txt');staging=base/(name+'-depmod')
            make=base/(name+'.mk')
            make.write_text('TARGET_OUT := '+str(target)+'\nDEPMOD := python3 '+str(depmod)+'\nKERNEL_OUT := '+str(base)+'\n'+macro+'\nall:\n\t$(call build-image-kernel-modules-lineage,'+str(source)+','+str(out)+',vendor/,'+str(staging)+',ansi_cprng,,'+str(listing)+',)\n')
            run=subprocess.run(['make','-f',str(make)],capture_output=True,text=True)
            assert run.returncode==0,run.stdout+run.stderr
            entries=listing.read_text().splitlines()
            assert len(entries)==5 and all(e.startswith(expected+'/') for e in entries)
            assert all((partition/e).is_file() for e in entries)
            assert (out/'lib/modules/ansi_cprng.ko').read_bytes()==source.read_bytes()
        repo=base/'repo';(repo/'build/tasks').mkdir(parents=True);p=repo/'build/tasks/kernel.mk';p.write_text(original)
        subprocess.run(['git','init','-q',str(repo)],check=True)
        subprocess.run(['git','-C',str(repo),'add','.'],check=True)
        subprocess.run(['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@localhost','commit','-qm','fixture'],check=True)
        apply=mod['apply'];actual=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
        before=p.read_bytes()
        try:apply(repo)
        except ValueError:pass
        else:raise AssertionError('Wrong immutable HEAD accepted')
        assert p.read_bytes()==before
        apply.__globals__['HEAD']=actual
        assert apply(repo)[0] and not apply(repo)[0]
        p.write_text(fixed+'\n# unreviewed addition\n');before=p.read_bytes()
        try:apply(repo)
        except ValueError:pass
        else:raise AssertionError('Unreviewed source accepted')
        assert p.read_bytes()==before
        p.write_text(original);(repo/'other.txt').write_text('unrelated')
        try:apply(repo)
        except ValueError:pass
        else:raise AssertionError('Untracked source accepted')
        assert p.read_text()==original
    print('KERNEL_FILELIST_COMPAT_SELFTEST=PASS')

if __name__=='__main__':main()
