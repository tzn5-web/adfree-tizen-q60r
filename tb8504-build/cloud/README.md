# Azure build controls

Canonical Android kernel, modules and images are built together in the audited Azure workspace. Never mix previous local artifacts with cloud artifacts. Local Android compilation remains stopped. No flash or pay-as-you-go upgrade.

The monitor runs every five minutes and latches a budget stop before terminating at USD 180 of the conservative USD 0.70/hour estimate. Azure billing is delayed; this estimate includes elapsed time while the VM is off and is intentionally conservative. A budget stop requires explicit operator review before restart. Invalid budget settings also stop computation.

The wrapper is pinned to an immutable reviewed tooling commit with successful CI. Update all three pin occurrences together only after the new exact commit passes CI. Artifact collection requires a successful current-run ROM report, final audit gates and matching source/tooling hashes. Collection creates an atomic per-report bundle, excludes stale/unbound files and verifies copies. Stop signals cannot produce a success record.

Self-deallocation uses only the VM identity and its existing own-VM read/deallocate role. A lock prevents concurrent duplicate requests; accepted requests are remembered per boot. HTTP 202 means accepted; confirm actual deallocation through Azure. Retained disks and IP resources can continue billing after compute stops.

When deploying during a build, preserve the original running Bash file. Deploy the audited wrapper under its separate filename, replace Python control files atomically and reload the unit without restarting it. Confirm unchanged MainPID, healthy service and active monitor. Never extract a full package over an active build.

Fixture validation: `python3 tb8504-build/cloud/self-test-cloud-controls.py`. Tests mock all service stops and Azure requests. They cover interrupted/incomplete wrappers, persistent credit stops, invalid budget settings, duplicate shutdown requests and stale/corrupt/old artifact rejection.
