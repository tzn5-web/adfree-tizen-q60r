#!/usr/bin/env python3
"""Fixtures only: never contact Azure, stop a service or compile Android."""
import json, os, re, runpy, subprocess, tempfile, time
from pathlib import Path
from unittest.mock import patch
import cloud_control as control

def rejects(call):
    try: call()
    except (ValueError, FileNotFoundError): return
    raise AssertionError('Unsafe input accepted')

def main():
    here=Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix='tb8504-cloud-control-') as temp:
        base=Path(temp)/'cloud'; base.mkdir()
        budget={'started_unix':1000,'upper_rate_usd_per_hour':0.70,'initial_spend_usd':0,'ceiling_usd':180}
        control.atomic_json(base/'budget.json',budget)
        assert abs(control.start_gate(base,4600)-0.70)<1e-9
        rejects(lambda:control.start_gate(base,1000+3600*260))
        control.atomic_json(base/'budget-stop.json',{'reason':'trial-credit-guard'})
        rejects(lambda:control.start_gate(base,4600))
        (base/'budget-stop.json').unlink()
        control.atomic_json(base/'deallocate.json',{'http_status':202,'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()})
        rejects(lambda:control.start_gate(base,4600))
        (base/'deallocate.json').unlink()
        budget['ceiling_usd']=200;control.atomic_json(base/'budget.json',budget)
        rejects(lambda:control.start_gate(base,4600))
        budget['ceiling_usd']=180;control.atomic_json(base/'budget.json',budget)
        assert (base/'budget.json').stat().st_mode & 0o777 == 0o644
        assert not list(base.glob('.budget.json.*'))
        root=Path(temp)/'android'; product=root/'out/target/product/TB8504'; product.mkdir(parents=True)
        home=Path(temp)/'home';home.mkdir()
        report=home/'TB8504_AUTOPILOT_20261010_fixture';report.mkdir()
        control.atomic_json(report/'STATUS.json',{'goal':'rom','success':True,'no_flash':True})
        (report/'FULL.log').write_text('FULL_ROM_STAGE=PASS\nFINAL_PACKAGE_AUDIT=PASS\nFINAL_ROM_POSTBUILD_SOURCE_GATES=PASS\nACTUAL_BUILT_OUTPUT_AUDIT=PASS\n')
        control.atomic_json(base/'run.json',{'started_unix':time.time()-10,'tooling_ref':'test-tooling'})
        state={'source_fingerprint':'test-source','tooling_ref':'test-tooling','images':{}}
        for kind in ('boot','recovery','system','rom'):
            name='lineage-fixture.zip' if kind=='rom' else kind+'.img'
            p=product/name;p.write_bytes((kind+'-validated').encode())
            row={'size':p.stat().st_size,'sha256':control.digest(p),'source_fingerprint':'test-source','tooling_ref':'test-tooling'}
            if kind=='rom':row['path']=str(p);state['rom']=row
            else:state['images'][kind]=row
        control.atomic_json(product/'.tb8504-autopilot-state.json',state)
        bundle=control.collect_artifacts(base,root,home)
        assert control.collect_artifacts(base,root,home)==bundle
        (product/'unbound-old.img').write_bytes(b'old');(product/'lineage-old.zip').write_bytes(b'old')
        assert not (bundle/'unbound-old.img').exists() and not (bundle/'lineage-old.zip').exists()
        (bundle/'boot.img').write_bytes(b'corrupt')
        rejects(lambda:control.collect_artifacts(base,root,home))
        (bundle/'boot.img').write_bytes((product/'boot.img').read_bytes())
        state['images']['boot']['source_fingerprint']='stale';control.atomic_json(product/'.tb8504-autopilot-state.json',state)
        rejects(lambda:control.collect_artifacts(base,root,home))
        guard=runpy.run_path(str(here/'credit-guard.py'))
        fake=subprocess.CompletedProcess([],0,'active\n','')
        seen=[]
        def mocked_run(argv,**kwargs):
            seen.append(argv)
            if argv[:2]==['systemctl','stop']: assert (base/'budget-stop.json').exists()
            return fake
        with patch.dict(guard['main'].__globals__,BASE=base), patch('cloud_control.budget_estimate',return_value=(budget,260,182)):
            # Function imported its reference before this patch; bind directly.
            with patch.dict(guard['main'].__globals__,budget_estimate=lambda:(budget,260,182)),patch('subprocess.run',side_effect=mocked_run):guard['main']()
        assert seen[-2:]==[['systemctl','stop','tb8504-build.service'],['/usr/local/sbin/tb8504-deallocate','trial-credit-guard']]
        seen.clear()
        with patch.dict(guard['main'].__globals__,BASE=base,budget_estimate=lambda:(_ for _ in ()).throw(ValueError('invalid fixture'))),patch('subprocess.run',side_effect=mocked_run):
            guard['main']()
        assert json.loads((base/'budget-stop.json').read_text())['reason']=='invalid-budget-configuration'
        assert seen[0]==['systemctl','stop','tb8504-build.service']
        helper=runpy.run_path(str(here/'tb8504-deallocate'))
        class Reply:
            status=202
            def __init__(self,data=b''):self.data=data
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self,*args):return self.data
        requests=[]
        def mocked_urlopen(req,**kwargs):
            requests.append(req.full_url)
            return Reply(b'{"access_token":"fixture-token"}') if '169.254.169.254' in req.full_url else Reply()
        with patch.dict(helper['main'].__globals__,BASE=base),patch('urllib.request.urlopen',side_effect=mocked_urlopen):
            helper['main']('finish-0'); helper['main']('finish-0')
        assert len(requests)==2
        assert json.loads((base/'deallocate.json').read_text())['reason']=='trial-credit-guard'
        assert 'fixture-token' not in (base/'deallocate.json').read_text()
        # Exercise the actual wrapper's signal/EXIT handling in a harmless sandbox.
        wrapper=(here/'server-build-audited.sh').read_text()
        pins=re.findall(r'(?:fetch origin |checkout --detach |TB8504_TOOLING_REF=|start-run )([0-9a-f]{40})',wrapper)
        assert len(pins)==4 and len(set(pins))==1, 'Fetch/checkout/export/run-binding tooling pins differ'
        prefix=wrapper[wrapper.index('completed=0'):wrapper.index('phase dependencies')]
        fakepackage=base/'package';fakepackage.mkdir()
        (fakepackage/'cloud_control.py').write_text('import sys\nprint("CONTROL="+" ".join(sys.argv[1:]))\n')
        prefix=prefix.replace('sudo /usr/local/sbin/tb8504-deallocate finish-$rc','echo STOP_REASON=finish-$rc')
        script='set -euo pipefail\nPACKAGE='+str(fakepackage)+'\n'+prefix+'\nkill -TERM $$\n'
        run=subprocess.run(['bash','-c',script],capture_output=True,text=True)
        assert run.returncode==143 and 'CONTROL=finish 143 0' in run.stdout
        script='set -euo pipefail\nPACKAGE='+str(fakepackage)+'\n'+prefix+'\ntrue\n'
        run=subprocess.run(['bash','-c',script],capture_output=True,text=True)
        assert run.returncode==1 and 'CONTROL=finish 1 0' in run.stdout
    subprocess.run(['bash','-n',str(here/'server-build-audited.sh')],check=True)
    subprocess.run(['bash','-n',str(here/'install-services.sh')],check=True)
    print('CLOUD_CONTROLS_SELFTEST=PASS')

if __name__=='__main__':main()
