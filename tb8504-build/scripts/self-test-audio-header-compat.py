#!/usr/bin/env python3
import importlib.util
import subprocess
import tempfile
from pathlib import Path

def main():
    spec = importlib.util.spec_from_file_location('audio_compat', Path(__file__).with_name('apply-audio-header-compat.py'))
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        def git(*args):
            return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.DEVNULL)
        git('init', '-q')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        for rel, count in helper.FILES.items():
            path = root / rel
            path.parent.mkdir(parents=True)
            path.write_text(('include $(CLEAR_VARS)\nLOCAL_HEADER_LIBRARIES := libhardware_headers\n'
                             'LOCAL_MODULE := preserved_audio\ninclude $(BUILD_SHARED_LIBRARY)\n') * count)
        (root / 'unrelated.c').write_text('int untouched;\n')
        git('add', '.')
        git('commit', '-qm', 'Fixture')
        original = {rel: (root / rel).read_bytes() for rel in helper.FILES}
        try:
            helper.apply(root)
        except ValueError:
            pass
        else:
            raise AssertionError('Wrong HEAD accepted')
        assert original == {rel: (root / rel).read_bytes() for rel in helper.FILES}
        helper.HEAD = git('rev-parse', 'HEAD').decode().strip()
        # An invalid second file must leave the valid first file untouched.
        (root / 'post_proc/Android.mk').write_text('unexpected layout\n')
        try:
            helper.apply(root)
        except ValueError:
            pass
        else:
            raise AssertionError('Unreviewed layout accepted')
        assert (root / 'hal/Android.mk').read_bytes() == original['hal/Android.mk']
        for rel, data in original.items():
            (root / rel).write_bytes(data)
        (root / 'unrelated.c').write_text('int changed;\n')
        try:
            helper.apply(root)
        except ValueError:
            pass
        else:
            raise AssertionError('Unrelated source difference accepted')
        (root / 'unrelated.c').write_text('int untouched;\n')
        changed, patch, sha = helper.apply(root)
        assert changed == 2 and patch.count('+LOCAL_HEADER_LIBRARIES') == 3
        for rel, data in original.items():
            result = (root / rel).read_text()
            assert result.replace(' generated_kernel_headers', '') == data.decode()
        assert helper.apply(root) == (0, '', sha)
    print('AUDIO_HEADER_COMPAT_SELFTEST=PASS')

if __name__ == '__main__':
    main()
