#!/usr/bin/env python3
"""Generate the QtQuick 6.11 backport from the pinned source and prior backports."""

import argparse
import difflib
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile

GUEST = Path(__file__).resolve().parents[1]
BACKPORT_ID = 'shell-qtquick-611'
BARE = re.compile(rb'^import QtQuick[^\S\r\n]*$', re.MULTILINE)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    spec_path = GUEST / 'spec.json'
    spec = json.loads(spec_path.read_text())
    checksum = hashlib.sha256()
    checksum.update(b'omarchy-git-tree-sha256-v1:path-nul-mode-nul-length-nul-content-nul\0')
    files = sorted((p for p in source.rglob('*') if (p.is_file() or p.is_symlink())
                    and '.git' not in p.relative_to(source).parts),
                   key=lambda p: str(p.relative_to(source)).encode())
    for path in files:
        data = os.readlink(path).encode() if path.is_symlink() else path.read_bytes()
        mode = b'120000' if path.is_symlink() else b'100755' if path.stat().st_mode & 0o111 else b'100644'
        checksum.update(str(path.relative_to(source)).encode() + b'\0' + mode + b'\0'
                        + str(len(data)).encode() + b'\0' + data + b'\0')
    if checksum.hexdigest() != spec['upstream']['treeSha256']:
        raise SystemExit('Source does not match the pinned normalized upstream tree')
    module_spec = importlib.util.spec_from_file_location('backports', GUEST / 'scripts/apply-omarchy-backports.py')
    backports = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(backports)
    prior = [hook for hook in spec['authenticity']['backports'] if hook['id'] != BACKPORT_ID]
    preimages_path = GUEST / 'migrations/preimages.json'
    preimages = json.loads(preimages_path.read_text())
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        tree = root / 'usr/share/omarchy'
        shutil.copytree(source, tree, symlinks=True, ignore=shutil.ignore_patterns('.git'))
        known = {}

        def remember():
            for path in (tree / 'shell').rglob('*.qml'):
                if BARE.search(path.read_bytes()):
                    relative = str(path.relative_to(tree))
                    known.setdefault(relative, set()).add(digest(path.read_bytes()))

        remember()
        for hook in prior:
            backports.apply_backport(GUEST, root, tree, hook)
            remember()
        power_hooks = json.loads((GUEST / 'native-overlay/usr/local/share/try-omarchy/power-profile-hooks.json').read_text())
        for hook in power_hooks:
            relative = hook['path'].removeprefix('usr/share/omarchy/')
            if relative not in known:
                continue
            current = (tree / relative).read_text()
            for version in [hook, *hook.get('previousVersions', [])]:
                text = current
                for old, new in reversed(version['replacements']):
                    if text.count(new) != 1:
                        raise SystemExit('Cannot reconstruct historical power preimage: ' + relative)
                    text = text.replace(new, old)
                if digest(text.encode()) != version['beforeSha256']:
                    raise SystemExit('Historical power preimage digest mismatch: ' + relative)
                known[relative].add(digest(text.encode()))
        targets, patch = [], []
        fixture = GUEST / 'tests/fixtures/shell-qtquick-before.tar.gz'
        with fixture.open('wb') as stream, gzip.GzipFile(filename='', mode='wb', fileobj=stream, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode='w') as archive:
                for relative in sorted(known):
                    before = (tree / relative).read_bytes()
                    after = BARE.sub(b'import QtQuick 6.11', before)
                    targets.append({'path': relative, 'beforeSha256': digest(before), 'afterSha256': digest(after)})
                    patch.append(f'diff --git a/{relative} b/{relative}\n')
                    patch.extend(difflib.unified_diff(before.decode().splitlines(keepends=True),
                                 after.decode().splitlines(keepends=True), fromfile='a/' + relative, tofile='b/' + relative))
                    destination = 'usr/share/omarchy/' + relative
                    preimages[destination] = sorted(known[relative])
                    info = tarfile.TarInfo(relative)
                    info.size, info.mode = len(before), 0o644
                    archive.addfile(info, io.BytesIO(before))
        patch_path = GUEST / 'patches/omarchy/shell-qtquick-611.patch'
        patch_path.write_text(''.join(patch))
        spec['authenticity']['backports'] = [*prior, {
            'id': BACKPORT_ID,
            'description': 'Pin shell QtQuick imports to 6.11 to avoid the Qt 6.12 Color singleton collision.',
            'reference': 'https://github.com/omacom/omarchy/pull/14845',
            'patch': str(patch_path.relative_to(GUEST)),
            'patchSha256': digest(patch_path.read_bytes()),
            'targets': targets,
        }]
        spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + '\n')
        preimages_path.write_text(json.dumps(preimages, indent=2, sort_keys=True) + '\n')
        components = GUEST / 'scripts/boot-fix-components.py'
        text = components.read_text()
        start, end = text.index('SHELL_TARGETS = '), text.index('POWER_TARGETS = ')
        paths = ''.join(f"    'usr/share/omarchy/{relative}',\n" for relative in sorted(known))
        components.write_text(text[:start] + 'SHELL_TARGETS = frozenset({\n' + paths + '})\n' + text[end:])
        print(f'Generated {len(targets)} shell import targets from {spec["upstream"]["commit"]}')


if __name__ == '__main__':
    main()
