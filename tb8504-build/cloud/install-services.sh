#!/usr/bin/env bash
set -euo pipefail
BASE=/home/dre/tb8504-cloud
PACKAGE=$BASE/package
install -m 755 "$PACKAGE/tb8504-deallocate" /usr/local/sbin/tb8504-deallocate
cat > /etc/systemd/system/tb8504-build.service <<'UNIT'
[Unit]
Description=TB8504 Android 16 cloud source sync and staged ROM build
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=dre
Group=dre
WorkingDirectory=/home/dre/tb8504-cloud
ExecStartPre=/usr/bin/python3 /home/dre/tb8504-cloud/package/cloud_control.py start-gate
ExecStart=/bin/bash /home/dre/tb8504-cloud/package/server-build-audited.sh
Restart=no
TimeoutStopSec=120
KillMode=control-group
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/tb8504-monitor.service <<'UNIT'
[Unit]
Description=Check TB8504 build health and trial budget estimate
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /home/dre/tb8504-cloud/package/credit-guard.py
UNIT
cat > /etc/systemd/system/tb8504-monitor.timer <<'UNIT'
[Unit]
Description=Periodic server-side TB8504 trial and build check
[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
Persistent=true
[Install]
WantedBy=timers.target
UNIT
python3 - "$BASE" <<'PY'
import json,sys,time
from pathlib import Path
base=Path(sys.argv[1]); p=base/'budget.json'
if not p.exists():
 plan=json.loads((base/'package/connection-plan.json').read_text())
 p.write_text(json.dumps({'started_unix':plan['provisioned_unix'],'upper_rate_usd_per_hour':0.70,'initial_spend_usd':0,'ceiling_usd':180,'trial_credit_usd':200,'remaining_reserve_usd':20},indent=2))
PY
chown dre:dre "$BASE/budget.json"
systemctl daemon-reload
systemctl enable tb8504-monitor.timer
# Build deliberately starts only after identity authorization is verified.
echo SERVER_MONITOR_INSTALLED=PASS
