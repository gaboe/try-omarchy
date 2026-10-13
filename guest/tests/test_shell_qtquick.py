import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import test_settings_install as support

GUEST = support.GUEST
FIXTURE = GUEST / 'tests/fixtures/shell-qtquick-before.tar.gz'
BARE_IMPORT = re.compile(rb'^import QtQuick[^\S\r\n]*$', re.MULTILINE)


def seed_shell(root):
    with tarfile.open(FIXTURE) as archive:
        for member in archive.getmembers():
            target = root / 'usr/share/omarchy' / member.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(member).read())
    return sorted((root / 'usr/share/omarchy/shell').rglob('*.qml'))


class ShellQtQuickTests(unittest.TestCase):
    def test_factory_backports_hide_qt612_color_in_every_shell_file(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / 'root'
            files = seed_shell(root)
            spec = json.loads((GUEST / 'spec.json').read_text())
            spec['authenticity']['backports'] = [
                hook for hook in spec['authenticity']['backports']
                if hook['id'] == 'shell-qtquick-611'
            ]
            (base / 'spec.json').write_text(json.dumps(spec))
            shutil.copytree(GUEST / 'patches', base / 'patches')
            before = {p: p.read_bytes() for p in files}
            result = subprocess.run([
                'python3', str(GUEST / 'scripts/apply-omarchy-backports.py'),
                '--root', str(root), '--spec', str(base / 'spec.json'),
            ], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            bare = [str(p.relative_to(root)) for p in files if BARE_IMPORT.search(p.read_bytes())]
            self.assertEqual([], bare, 'Factory shell still imports QtQuick without a version')
            for path, data in before.items():
                self.assertEqual(BARE_IMPORT.sub(b'import QtQuick 6.11', data), path.read_bytes())

    def test_boot_repair_applies_repeats_and_survives_runtime_reinstallation(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = support.SettingsInstallTests().make_payload(directory)
            root = Path(directory) / 'root'
            files = seed_shell(root)
            before = {p: p.read_bytes() for p in files}
            result = support.fixes.migrate(payload, root, True, '')
            bare = [str(p.relative_to(root)) for p in files if BARE_IMPORT.search(p.read_bytes())]
            self.assertEqual([], bare, 'Retained VM shell still imports QtQuick without a version')
            self.assertEqual('applied', result['components']['shell-qtquick'])
            for path, data in before.items():
                self.assertEqual(BARE_IMPORT.sub(b'import QtQuick 6.11', data), path.read_bytes())
            inodes = {p: p.stat().st_ino for p in files}
            result = support.fixes.migrate(payload, root, True, '')
            self.assertEqual('current', result['components']['shell-qtquick'])
            self.assertEqual('current', result['components']['power'])
            self.assertEqual(inodes, {p: p.stat().st_ino for p in files})
            seed_shell(root)
            result = support.fixes.migrate(payload, root, True, '')
            self.assertEqual('applied', result['components']['shell-qtquick'])
            self.assertFalse(any(BARE_IMPORT.search(p.read_bytes()) for p in files))

    def test_hand_repaired_shell_is_current(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = support.SettingsInstallTests().make_payload(directory)
            root = Path(directory) / 'root'
            files = seed_shell(root)
            for path in files:
                path.write_bytes(BARE_IMPORT.sub(b'import QtQuick 6.11', path.read_bytes()))
            inodes = {p: p.stat().st_ino for p in files}
            result = support.fixes.migrate(payload, root, True, '')
            self.assertEqual('current', result['components'].get('shell-qtquick'), result)
            self.assertEqual(inodes, {p: p.stat().st_ino for p in files})

    def test_skip_preserves_shell_and_custom_file_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = support.SettingsInstallTests().make_payload(directory)
            root = Path(directory) / 'root'
            files = seed_shell(root)
            target = files[0]
            custom = target.read_bytes() + b'// personal change\n'
            target.write_bytes(custom)
            before = {p: p.read_bytes() for p in files}
            result = support.fixes.migrate(payload, root, False, '')
            self.assertEqual(before, {p: p.read_bytes() for p in files})
            self.assertEqual('pending', result['components'].get('shell-qtquick'), result)
            result = support.fixes.migrate(payload, root, True, '')
            self.assertEqual('complete', result['state'])
            self.assertEqual(custom, target.read_bytes())
            self.assertFalse(any(BARE_IMPORT.search(p.read_bytes()) for p in files[1:]))

    def test_power_and_shell_repairs_share_one_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = support.SettingsInstallTests().make_payload(directory)
            root = Path(directory) / 'root'
            files = seed_shell(root)
            hooks = json.loads((payload / 'power-profile-hooks.json').read_text())
            for hook in hooks:
                target = root / hook['path']
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.suffix == '.qml':
                    text = target.read_text()
                    for old, new in reversed(hook['replacements']):
                        text = text.replace(new, old)
                    target.write_text(text)
                else:
                    target.write_bytes((GUEST / 'tests/fixtures/power-profile' / target.name).read_bytes())
            result = support.fixes.migrate(payload, root, True, '')
            self.assertEqual('complete', result['state'])
            self.assertEqual('applied', result['components']['power'])
            self.assertEqual('applied', result['components']['shell-qtquick'])
            self.assertFalse(any(BARE_IMPORT.search(p.read_bytes()) for p in files))
            result = support.fixes.migrate(payload, root, True, '')
            self.assertEqual('current', result['components']['power'])
            self.assertEqual('current', result['components']['shell-qtquick'])

    def test_unknown_and_symlinked_files_are_preserved_without_failure(self):
        for unsafe in (False, True):
            with self.subTest(symlink=unsafe), tempfile.TemporaryDirectory() as directory:
                payload = support.SettingsInstallTests().make_payload(directory)
                root = Path(directory) / 'root'
                target = root / 'usr/share/omarchy/shell/Commons/Border.qml'
                target.parent.mkdir(parents=True)
                custom = b'import QtQuick\n// personal shell\n'
                if unsafe:
                    outside = Path(directory) / 'custom.qml'
                    outside.write_bytes(custom)
                    target.symlink_to(outside)
                else:
                    target.write_bytes(custom)
                result = support.fixes.migrate(payload, root, True, '')
                self.assertEqual('complete', result['state'])
                self.assertEqual('preserved', result['components']['shell-qtquick'])
                self.assertEqual(custom, target.read_bytes())
                self.assertEqual(unsafe, target.is_symlink())

    def test_interrupted_shell_write_recovers_even_when_next_boot_skips(self):
        class PowerLoss(BaseException):
            pass

        with tempfile.TemporaryDirectory() as directory:
            payload = support.SettingsInstallTests().make_payload(directory)
            root = Path(directory) / 'root'
            files = seed_shell(root)
            before = {p: p.read_bytes() for p in files}
            writer = support.fixes.write_file

            def interrupted(path, value, *args):
                if path.name == 'Color.qml':
                    raise PowerLoss()
                return writer(path, value, *args)

            with patch.object(support.fixes, 'write_file', side_effect=interrupted):
                with self.assertRaises(PowerLoss):
                    support.fixes.migrate(payload, root, True, '')
            self.assertNotEqual(before[files[0]], files[0].read_bytes())
            result = support.fixes.migrate(payload, root, False, '')
            self.assertEqual('skipped', result['state'])
            self.assertEqual(before, {p: p.read_bytes() for p in files})
            self.assertFalse((root / support.fixes.STATE / 'journal.json').exists())


if __name__ == '__main__':
    unittest.main()
