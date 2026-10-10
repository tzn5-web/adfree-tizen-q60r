#!/usr/bin/env python3
"""Bind the pinned legacy audio Make modules to Lineage generated headers."""
import argparse
import difflib
import hashlib
import subprocess
from pathlib import Path

HEAD = '560c079979144611f90e32f5a7485e631ef022c9'
FILES = {'hal/Android.mk': 1, 'post_proc/Android.mk': 2,
         'hal/audio_extn/spkr_protection.c': None}
OLD = 'LOCAL_HEADER_LIBRARIES := libhardware_headers'
NEW = OLD + ' generated_kernel_headers'

def transform(text, count):
    if count is None:
        for name in ('spkr_calibration_thread', 'spkr_v_vali_thread'):
            old = f'static void* {name}()\n{{\n'
            new = f'static void* {name}(void *arg)\n{{\n    (void)arg;\n'
            if text.count(old) != 1:
                raise ValueError('Unexpected speaker thread layout: ' + name)
            text = text.replace(old, new)
        return text
    if text.count(OLD) != count or 'generated_kernel_headers' in text:
        raise ValueError('Unexpected audio Make header layout')
    return text.replace(OLD, NEW)

def apply(audio):
    def git(*args):
        return subprocess.check_output(['git', *args], cwd=audio)
    if git('rev-parse', 'HEAD').decode().strip() != HEAD:
        raise ValueError('Audio HEAD differs from the approved source')
    if git('ls-files', '--others', '--exclude-standard').strip():
        raise ValueError('Unexpected untracked audio source files')
    dirty = set(git('diff', '--name-only', 'HEAD').decode().splitlines())
    if not dirty.issubset(FILES):
        raise ValueError('Unexpected audio patch paths')
    if git('diff', '--cached', '--name-only').strip():
        raise ValueError('Unexpected staged audio changes')
    pending = []
    patch = []
    for rel, count in FILES.items():
        original = git('show', 'HEAD:' + rel).decode()
        desired = transform(original, count)
        path = audio / rel
        current = path.read_text()
        if current not in (original, desired):
            raise ValueError('Unreviewed audio file contents: ' + rel)
        if current != desired:
            pending.append((path, desired))
            patch.extend(difflib.unified_diff(current.splitlines(True), desired.splitlines(True),
                         fromfile='a/' + rel, tofile='b/' + rel))
    # Validate every file before any mutation. Pin Git's diff formatting too.
    for path, desired in pending:
        path.write_text(desired)
    subprocess.run(['git', 'config', '--local', 'core.abbrev', '40'], cwd=audio, check=True)
    patch_sha = hashlib.sha256(git('diff', '--binary', 'HEAD')).hexdigest()
    return len(pending), ''.join(patch), patch_sha

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--audio', required=True, type=Path)
    parser.add_argument('--patch-out', required=True, type=Path)
    parser.add_argument('--report-out', required=True, type=Path)
    args = parser.parse_args()
    try:
        changed, patch, sha = apply(args.audio)
    except (ValueError, subprocess.CalledProcessError) as exc:
        raise SystemExit('AUDIO_HEADER_COMPAT_FAIL=' + str(exc))
    args.patch_out.write_text(patch)
    report = ('AUDIO_HEADER_COMPAT_STATE=' + ('APPLIED' if changed else 'ALREADY_APPLIED') + '\n'
              + f'CHANGED_FILES={changed}\nAUDIO_PATCH_SHA256={sha}\nAUDIO_HEADER_COMPAT=PASS\n')
    args.report_out.write_text(report)
    print(report, end='')

if __name__ == '__main__':
    main()
