#!/usr/bin/env python3
"""Record budget/health atomically and latch a trial stop before terminating."""
import os, shutil, subprocess, time
from cloud_control import BASE, atomic_json, budget_estimate

def main():
    try:
        budget, elapsed, estimated = budget_estimate()
    except (ValueError, OSError, KeyError) as exc:
        atomic_json(BASE / 'budget-stop.json', {'reason': 'invalid-budget-configuration', 'error': str(exc), 'time_unix': time.time()})
        subprocess.run(['systemctl', 'stop', 'tb8504-build.service'], check=True)
        subprocess.run(['/usr/local/sbin/tb8504-deallocate', 'invalid-budget-configuration'], check=True)
        return
    service = subprocess.run(['systemctl', 'is-active', 'tb8504-build.service'], capture_output=True, text=True).stdout.strip()
    status = {'time_unix': time.time(), 'elapsed_hours': round(elapsed, 3),
              'estimated_spend_upper_usd': round(estimated, 2), 'credit_guard_ceiling_usd': budget['ceiling_usd'],
              'estimate_is_actual_billing': False, 'free_disk_bytes': shutil.disk_usage(BASE).free,
              'load_average': os.getloadavg(), 'build_service': service}
    atomic_json(BASE / 'monitor.json', status)
    if estimated >= budget['ceiling_usd'] or (BASE / 'budget-stop.json').exists():
        if not (BASE / 'budget-stop.json').exists():
            atomic_json(BASE / 'budget-stop.json', {'reason': 'trial-credit-guard', **status})
        subprocess.run(['systemctl', 'stop', 'tb8504-build.service'], check=True)
        subprocess.run(['/usr/local/sbin/tb8504-deallocate', 'trial-credit-guard'], check=True)
    elif (BASE / 'build-started').exists() and service not in ('active', 'activating', 'deactivating'):
        subprocess.run(['/usr/local/sbin/tb8504-deallocate', 'build-service-ended'], check=True)

if __name__ == '__main__':
    main()
