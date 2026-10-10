#!/usr/bin/env python3
"""Correct relative kernel-module paths for vendor embedded in system."""
import argparse,difflib,hashlib,shutil,subprocess
from pathlib import Path
HEAD='686d8669737d2207208ea21075320840b5ec8463'
FILE='build/tasks/kernel.mk'

def transform(text):
    start='    if [ -n "$(7)" ]; then \\\n'
    end='        sort -u "$(7)" -o "$(7)"; \\\n'
    if text.count(start)!=1 or text.count(end)!=1:raise ValueError('Unexpected kernel file-list macro layout')
    a=text.index(start);b=text.index(end,a)+len(end);block=text[a:b]
    if block.count('echo lib/modules')!=5:raise ValueError('Unexpected kernel module manifest entries')
    fixed=block.replace('echo lib/modules','echo $(if $(filter $(TARGET_OUT)/vendor,$(2)),vendor/)lib/modules')
    return text[:a]+fixed+text[b:]

def apply(repo):
    def git(*args):return subprocess.check_output(['git',*args],cwd=repo)
    if git('rev-parse','HEAD').decode().strip()!=HEAD:raise ValueError('Unapproved Lineage source HEAD')
    if git('ls-files','--others','--exclude-standard').strip():raise ValueError('Untracked Lineage source files')
    if git('diff','--cached','--name-only').strip():raise ValueError('Staged Lineage changes')
    if not set(git('diff','--name-only','HEAD').decode().splitlines()).issubset({FILE}):raise ValueError('Unrelated Lineage changes')
    original=git('show','HEAD:'+FILE).decode();desired=transform(original);path=repo/FILE;current=path.read_text()
    if current not in (original,desired):raise ValueError('Unreviewed kernel Make contents')
    changed=current!=desired
    if changed:path.write_text(desired)
    subprocess.run(['git','config','--local','core.abbrev','40'],cwd=repo,check=True)
    patch=''.join(difflib.unified_diff(current.splitlines(True),desired.splitlines(True),fromfile='a/'+FILE,tofile='b/'+FILE))
    return changed,patch,hashlib.sha256(git('diff','--binary','HEAD')).hexdigest()

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--lineage',type=Path,required=True);ap.add_argument('--patch-out',type=Path,required=True);ap.add_argument('--report-out',type=Path,required=True);ap.add_argument('--refresh-file-list',action='store_true');args=ap.parse_args()
    root=args.lineage.resolve().parents[1]
    listing=root/'out/target/product/TB8504/obj/PACKAGING/system_intermediates/file_list.txt'
    if args.refresh_file_list:
        if args.lineage.resolve()!=root/'vendor/lineage':raise SystemExit('Unexpected Lineage source path')
        if listing.exists() and (listing.is_symlink() or not listing.is_file() or not listing.resolve().is_relative_to(root)):
            raise SystemExit('Unsafe image file-list path')
    try:changed,patch,sha=apply(args.lineage)
    except (ValueError,subprocess.CalledProcessError) as exc:raise SystemExit('KERNEL_FILELIST_COMPAT_FAIL='+str(exc))
    args.patch_out.write_text(patch)
    if changed and args.refresh_file_list and listing.exists():
        # Regenerate only invalidated packaging metadata, preserving compiled
        # outputs and recording the old list. Kernel's own rule appends the
        # corrected paths after Ninja recreates its base file list.
        shutil.copy2(listing,args.report_out.with_suffix('.old-file-list.txt'))
        listing.unlink()
    report='KERNEL_FILELIST_COMPAT_STATE='+('APPLIED' if changed else 'ALREADY_APPLIED')+f'\nCHANGED_FILES={int(changed)}\nLINEAGE_PATCH_SHA256={sha}\nKERNEL_FILELIST_COMPAT=PASS\n'
    args.report_out.write_text(report);print(report,end='')

if __name__=='__main__':main()
