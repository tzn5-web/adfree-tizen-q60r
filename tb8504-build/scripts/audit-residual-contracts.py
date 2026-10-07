#!/usr/bin/env python3
"""Cross-layer residual audit for TB8504 Android 16 bring-up."""
from __future__ import annotations
import argparse, re
from pathlib import Path

IMS_SERVICES = {
    "vendor.imsqmidaemon": "imsqmidaemon",
    "vendor.imsdatadaemon": "imsdatadaemon",
    "vendor.ims_rtp_daemon": "ims_rtp_daemon",
    "vendor.imsrcsservice": "imsrcsd",
}
ABSENT_SERVICES = {
    "audiod","chre","cnss-daemon","crashdata-sh","diag_mdlog_start",
    "diag_mdlog_stop","drmdiag","dts_configurator","dtseagleservice",
    "esepmdaemon","fstman","fstman_wlan0","gamed","hbtp","hvdcp",
    "ims_regmanager","iop","mdtpd","mlid","perfd","poweroffhandler","ppd",
    "ptt_ffbm","ptt_socket_app","qcamerasvr","qcomsysd","qfp-daemon",
    "qlogd","qrngd","qrngp","qseeproxydaemon","qvop-daemon",
    "seemp_healthd","ssgqmigd","ssgtzd","vendor.LKCore-dbg",
    "vendor.LKCore-rel","vendor.audio-hal-2-0","vendor.bt-dun",
    "vendor.bt_logger","vendor.btsnoop","vendor.dataadpl","vendor.hbtp",
    "vendor.hvdcp_opti","vendor.ipacm-diag","vendor.move_time_data",
    "vendor.port-bridge","vendor.qdmastatsd","vendor.qmuxd","vendor.qrtr-ns",
    "vendor.ril-daemon2","vendor.ril-daemon3","vendor.sensors",
    "vendor.ss_ramdump","vendor.ssr_diag","vendor.ssr_setup",
    "vendor.start_hci_filter","vendor.tlocd","vendor.vppservice",
    "vendor.wifilearner","vm_bms","wifi-crda","wifi-sdio-on","wifi_ftmd",
    "wigighalsvc","wigignpt",
}
SERVICE_RE=re.compile(r"^service\s+(\S+)\s+([^\s\\]+)")
CONTROL_RE=re.compile(r"^\s*(?:start|stop|restart|enable|disable)\s+([^\s#;]+)")
CTL_RE=re.compile(r"^\s*setprop\s+ctl\.(?:start|stop|restart)\s+([^\s#;]+)")
COPY_RE=re.compile(r"^\s*vendor/lenovo/TB8504/proprietary/[^:]+:([^\s\\]+)")

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--device",required=True,type=Path)
    ap.add_argument("--vendor",required=True,type=Path)
    args=ap.parse_args()
    d=args.device.resolve(); v=args.vendor.resolve()
    failures=[]

    services={}
    refs=[]
    for p in d.rglob("*"):
        if not p.is_file() or ".pre_" in p.name:
            continue
        if p.suffix in {".rc",".sh"}:
            for no,line in enumerate(p.read_text("utf-8",errors="replace").splitlines(),1):
                m=SERVICE_RE.match(line)
                if m:
                    services[m.group(1)]=(m.group(2),str(p.relative_to(d)),no)
                c=CONTROL_RE.match(line) or CTL_RE.match(line)
                if c and c.group(1) in ABSENT_SERVICES:
                    refs.append((str(p.relative_to(d)),no,c.group(1),line.strip()))

    vmk=(v/"TB8504-vendor.mk").read_text("utf-8",errors="replace")
    dests=set()
    for line in vmk.splitlines():
        m=COPY_RE.match(line)
        if m: dests.add(m.group(1))

    for svc,exe in IMS_SERVICES.items():
        row=services.get(svc)
        if not row:
            failures.append(f"missing IMS service {svc}")
            continue
        if not any(x.endswith("/bin/"+exe) for x in dests):
            failures.append(f"IMS executable not installed: {svc}->{exe}")

    if refs:
        failures.append(f"{len(refs)} control refs target intentionally absent services")

    vendor_text=vmk+"\n"+(v/"Android.bp").read_text("utf-8",errors="replace")
    for token in ("WfdService","WfdCommon"):
        if token in vendor_text:
            failures.append(f"stale WFD vendor module remains: {token}")

    dmk=(d/"device.mk").read_text("utf-8",errors="replace")
    if "fstman.ini" in dmk:
        failures.append("fstman.ini still copied although fstman service/binary is absent")

    prop=(d/"vendor_prop.mk").read_text("utf-8",errors="replace")
    if "persist.sys.wfd.virtual" in prop:
        failures.append("stale WFD property remains")

    overlay=(d/"overlay/frameworks/base/core/res/res/values/config.xml").read_text("utf-8",errors="replace")
    for name in ("config_wifiDisplaySupportsProtectedBuffers","config_enableWifiDisplay"):
        if f'<bool name="{name}">true</bool>' in overlay:
            failures.append(f"WFD framework overlay still enabled: {name}")

    for path in (d/"configs/qti_whitelist.xml",d/"configs/privapp-permissions-qti.xml"):
        text=path.read_text("utf-8",errors="replace")
        if "com.qualcomm.wfd." in text:
            failures.append(f"stale WFD package policy remains in {path.name}")

    manifest=(d/"manifest.xml").read_text("utf-8",errors="replace")
    if "vendor.qti.imsrtpservice" not in manifest:
        failures.append("IMS RTP VINTF declaration unexpectedly missing")

    print("=== TB8504 RESIDUAL CROSS-LAYER AUDIT ===")
    print(f"IMS_REQUIRED_SERVICES={len(IMS_SERVICES)}")
    print(f"IMS_PRESENT_SERVICES={sum(x in services for x in IMS_SERVICES)}")
    print(f"ABSENT_SERVICE_CONTROL_REFS={len(refs)}")
    for row in refs:
        print(f"STALE_CONTROL_REF={row[0]}:{row[1]}|{row[2]}|{row[3]}")
    print(f"RESIDUAL_FAILURES={len(failures)}")
    for f in failures: print(f"FAIL={f}")
    if failures:
        print("TB8504_RESIDUAL_AUDIT=NEEDS_REVIEW")
        return 1
    print("TB8504_RESIDUAL_AUDIT=PASS")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
