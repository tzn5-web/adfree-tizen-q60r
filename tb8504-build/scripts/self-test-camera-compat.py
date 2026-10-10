#!/usr/bin/env python3
"""Exercise vanilla/non-vanilla callback preprocessing and fail-closed edits."""
import runpy
import subprocess
import tempfile
from pathlib import Path


def main():
    scripts = Path(__file__).resolve().parent
    transform = runpy.run_path(str(scripts / 'apply-camera-compat.py'))['transform']
    source = '''int startPreview() {
    int rc = 0;
    standard_preview_callback();
    if ( msgTypeEnabled(CAMERA_MSG_META_DATA) && mCameraId == 0 ) {
        send_otp(CAMERA_MSG_META_DATA);
    }
    LOGI("X rc = %d", rc);
    return rc;
}
int32_t QCamera2HardwareInterface::processDualCameraUpdate(
        cam_reprocess_info_t repro_info) {
    int32_t rc = NO_ERROR;
    cam_dimension_t dim;

    if ( msgTypeEnabled(CAMERA_MSG_META_DATA) ) {
        send_dual(CAMERA_MSG_META_DATA);
    }
    return rc;
}
'''
    fixed = transform(source)
    assert transform(fixed) == fixed
    def preprocess(text, vanilla):
        args = ['cpp', '-P'] + (['-DVANILLA_HAL'] if vanilla else [])
        return subprocess.check_output(args, input=text.encode()).decode()
    vanilla = preprocess(fixed, True)
    assert 'CAMERA_MSG_META_DATA' not in vanilla
    assert 'standard_preview_callback();' in vanilla
    assert 'processDualCameraUpdate' in vanilla and vanilla.count('return rc;') == 2
    assert preprocess(fixed, False) == preprocess(source, False)
    for invalid in (source + source, source.replace('mCameraId == 0', 'mCameraId == 1'),
                    fixed.replace('#endif', '', 1)):
        try:
            transform(invalid)
        except (SystemExit, ValueError):
            pass
        else:
            raise AssertionError('unexpected layout accepted')
    with tempfile.TemporaryDirectory() as td:
        device = Path(td)
        camera = device / 'camera/QCamera2'
        (camera / 'HAL').mkdir(parents=True)
        (camera / 'Android.mk').write_text('LOCAL_CFLAGS += -DVANILLA_HAL\n')
        cpp = camera / 'HAL/QCamera2HWI.cpp'
        cpp.write_text(source)
        for stage in ('first', 'second'):
            subprocess.run(['python3', str(scripts / 'apply-camera-compat.py'),
                            '--device', str(device), '--patch-out', str(device / f'{stage}.patch'),
                            '--report-out', str(device / f'{stage}.txt')], check=True)
        assert (device / 'second.patch').stat().st_size == 0
        assert 'CAMERA_COMPAT_STATE=ALREADY_APPLIED' in (device / 'second.txt').read_text()
        (camera / 'Android.mk').write_text('LOCAL_CFLAGS += -Wall\n')
        before = cpp.read_bytes()
        result = subprocess.run(['python3', str(scripts / 'apply-camera-compat.py'),
                                 '--device', str(device), '--patch-out', str(device / 'bad.patch'),
                                 '--report-out', str(device / 'bad.txt')], capture_output=True)
        assert result.returncode != 0 and cpp.read_bytes() == before
    auto = runpy.run_path(str(scripts / 'tb8504-autopilot.py'))['Autopilot']
    fake = auto.__new__(auto)
    failure = "checkvintf license metadata\nerror: use of undeclared identifier 'CAMERA_MSG_META_DATA'"
    assert fake.classify_failure(failure, 'systemimage')[0] == 'camera_compat'
    print('CAMERA_COMPAT_SELF_TEST=PASS')


if __name__ == '__main__':
    main()
