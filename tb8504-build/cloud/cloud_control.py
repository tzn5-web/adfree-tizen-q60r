"""Fail-closed state and artifact controls for the Azure build."""
import hashlib, json, math, os, shutil, tempfile, time
from pathlib import Path
BASE = Path('/home/dre/tb8504-cloud')
ROOT = Path('/home/dre/android16-tb8504/lineage-23.2')

def atomic_json(path, payload):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(payload, stream, indent=2)
            stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)

def budget_estimate(base=BASE, now=None):
    budget = json.loads((base / 'budget.json').read_text())
    values = [budget.get(k) for k in ('started_unix', 'upper_rate_usd_per_hour', 'ceiling_usd')]
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in values):
        raise ValueError('Missing or invalid trial budget settings')
    initial = budget.get('initial_spend_usd', 0)
    if not isinstance(initial, (int, float)) or not math.isfinite(initial) or initial < 0:
        raise ValueError('Invalid initial spend')
    if budget['ceiling_usd'] > 180 or budget['upper_rate_usd_per_hour'] < 0.70:
        raise ValueError('Trial guard exceeds authorized ceiling or underestimates configured rate')
    elapsed = max(0, (time.time() if now is None else now) - budget['started_unix']) / 3600
    return budget, elapsed, initial + elapsed * budget['upper_rate_usd_per_hour']

def start_gate(base=BASE, now=None):
    budget, elapsed, estimated = budget_estimate(base, now)
    if (base / 'budget-stop.json').exists():
        raise ValueError('Prior trial-credit stop requires explicit review; automatic restart refused')
    if estimated >= budget['ceiling_usd']:
        raise ValueError('Trial credit ceiling reached; start refused')
    request = base / 'deallocate.json'
    if request.exists():
        record = json.loads(request.read_text())
        boot_id = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if record.get('boot_id') == boot_id and record.get('http_status') in (200, 202):
            raise ValueError('Deallocation already accepted in this boot; start refused')
    return estimated

def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): result.update(block)
    return result.hexdigest()

def completed_report(home, started_unix):
    for report in sorted(home.glob('TB8504_AUTOPILOT_*'), reverse=True):
        status_file = report / 'STATUS.json'
        if not report.is_dir() or not status_file.is_file() or status_file.stat().st_mtime < started_unix: continue
        status = json.loads(status_file.read_text())
        if status.get('goal') != 'rom' or status.get('success') is not True or status.get('no_flash') is not True: continue
        log = (report / 'FULL.log').read_text(errors='replace')
        required = ('FULL_ROM_STAGE=PASS', 'FINAL_PACKAGE_AUDIT=PASS', 'FINAL_ROM_POSTBUILD_SOURCE_GATES=PASS', 'ACTUAL_BUILT_OUTPUT_AUDIT=PASS')
        if not all(marker in log for marker in required): raise ValueError('ROM report lacks final audit gates')
        return report
    raise ValueError('No successful audited ROM report from this run')

def collect_artifacts(base=BASE, root=ROOT, home=Path('/home/dre')):
    run = json.loads((base / 'run.json').read_text())
    report = completed_report(home, run['started_unix'])
    product = root / 'out/target/product/TB8504'
    state = json.loads((product / '.tb8504-autopilot-state.json').read_text())
    fingerprint, tooling = state.get('source_fingerprint'), state.get('tooling_ref')
    if not fingerprint or tooling != run['tooling_ref']: raise ValueError('Artifacts not bound to current tooling/source run')
    rows = [(product / (kind + '.img'), state.get('images', {}).get(kind, {})) for kind in ('boot', 'recovery', 'system')]
    vendor = state.get('images', {}).get('vendor')
    if vendor and vendor.get('source_fingerprint') == fingerprint: rows.append((product / 'vendor.img', vendor))
    rom = state.get('rom', {})
    rom_path = Path(rom.get('path', ''))
    if rom_path.parent.resolve() != product.resolve() or not rom_path.name.startswith('lineage-') or rom_path.suffix != '.zip':
        raise ValueError('Final ROM path outside product directory')
    rows.append((rom_path, rom))
    for path, row in rows:
        if not path.is_file() or path.is_symlink() or path.stat().st_size <= 0: raise ValueError('Artifact missing: ' + str(path))
        if row.get('source_fingerprint') != fingerprint or row.get('tooling_ref') != tooling: raise ValueError('Stale artifact: ' + path.name)
        if row.get('size') != path.stat().st_size or row.get('sha256') != digest(path): raise ValueError('Hash/size mismatch: ' + path.name)
    artifacts = base / 'artifacts'; artifacts.mkdir(exist_ok=True)
    final = artifacts / report.name
    if final.exists():
        receipt = json.loads((final / 'receipt.json').read_text())
        if receipt.get('source_fingerprint') != fingerprint or receipt.get('tooling_ref') != tooling: raise ValueError('Existing bundle provenance mismatch')
        for path, row in rows:
            if digest(final / path.name) != row['sha256']: raise ValueError('Existing bundle corrupt')
        return final
    stage = Path(tempfile.mkdtemp(prefix='.collect-', dir=artifacts))
    try:
        manifest = []
        for path, row in rows:
            shutil.copy2(path, stage / path.name)
            if digest(stage / path.name) != row['sha256']: raise ValueError('Copy hash mismatch')
            manifest.append(row['sha256'] + '  ' + path.name)
        shutil.copytree(report, stage / 'report')
        shutil.copy2(product / '.tb8504-autopilot-state.json', stage / 'source-state.json')
        (stage / 'SHA256SUMS').write_text('\n'.join(manifest) + '\n')
        atomic_json(stage / 'receipt.json', {'source_fingerprint': fingerprint, 'tooling_ref': tooling, 'report': str(report),
                    'files': [p.name for p, _ in rows], 'verified_at_unix': time.time()})
        os.rename(stage, final)
    except BaseException:
        shutil.rmtree(stage); raise
    return final

if __name__ == '__main__':
    import sys
    if sys.argv[1:] == ['start-gate']: print('TRIAL_START_GATE=PASS estimated_upper_usd=' + str(round(start_gate(), 2)))
    elif sys.argv[1:] == ['collect']: print('VERIFIED_ARTIFACT_BUNDLE=' + str(collect_artifacts()))
    elif len(sys.argv) == 3 and sys.argv[1] == 'start-run':
        import re
        if not re.fullmatch('[0-9a-f]{40}', sys.argv[2]): raise SystemExit('Invalid tooling revision')
        start_gate()
        atomic_json(BASE / 'run.json', {'started_unix': time.time(), 'tooling_ref': sys.argv[2]})
    elif len(sys.argv) == 3 and sys.argv[1] == 'phase':
        atomic_json(BASE / 'state.json', {'phase': sys.argv[2], 'time_unix': time.time()})
    elif len(sys.argv) == 4 and sys.argv[1] == 'finish':
        rc, completed = int(sys.argv[2]), sys.argv[3] == '1'
        atomic_json(BASE / 'state.json', {'phase': 'finished' if rc == 0 and completed else 'interrupted' if rc in (130, 143) else 'failed',
                    'rc': rc, 'artifacts_verified': rc == 0 and completed, 'time_unix': time.time()})
    else: raise SystemExit('Expected start-gate or collect')
