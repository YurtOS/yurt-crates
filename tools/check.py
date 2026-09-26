"""Validate every published snapshot and immutable crate archive."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.index import CRATES_IO_INDEX, index_path, read_crate
from tools.publish import DOWNLOAD, _version_parts
from tools.page import render_page


def check_repository(root: Path, base_ref: str | None) -> list[str]:
    root = Path(root)
    errors = []
    latest_path = root / 'latest'
    try:
        latest = int(latest_path.read_text().strip())
        if latest < 1:
            raise ValueError
    except (OSError, ValueError):
        return ['latest must contain a positive snapshot number']

    previous_entries = {}
    snapshot_records = {}
    for number in range(1, latest + 1):
        snapshot = root / 'index' / str(number)
        if not snapshot.is_dir():
            errors.append(f'missing snapshot {number}')
            continue
        try:
            config = json.loads((snapshot / 'config.json').read_text())
            if config != {'dl': DOWNLOAD}:
                errors.append(f'snapshot {number}: config.json download URL mismatch')
            ports = json.loads((snapshot / 'ports.json').read_text())
            if not isinstance(ports, dict):
                raise ValueError('ports.json is not an object')
        except (OSError, json.JSONDecodeError, ValueError) as error:
            errors.append(f'snapshot {number}: invalid config or ports.json: {error}')
            continue

        records = []
        seen = set()
        for file in sorted(snapshot.rglob('*')):
            if not file.is_file() or file.name in {'config.json', 'ports.json'}:
                continue
            try:
                if file.relative_to(snapshot) != index_path(file.name):
                    errors.append(f'snapshot {number}: index path mismatch for {file.name}')
                for line_no, line in enumerate(file.read_text().splitlines(), 1):
                    record = json.loads(line)
                    name, version = record.get('name'), record.get('vers')
                    if not isinstance(name, str) or not isinstance(version, str):
                        raise ValueError('record missing name or version')
                    if file.relative_to(snapshot) != index_path(name):
                        errors.append(f'snapshot {number}: wrong index file for {name} {version}')
                    upstream, _ = _version_parts(version)
                    identity = (name.lower(), upstream)
                    if identity in seen:
                        errors.append(f'snapshot {number}: duplicate upstream version {name} {upstream}')
                    seen.add(identity)
                    for dep in record.get('deps', []):
                        if not isinstance(dep, dict) or not isinstance(dep.get('registry'), str):
                            errors.append(f'snapshot {number}: {name} {version} dependency missing registry')
                    archive = root / 'crates' / name / f'{name}-{version}.crate'
                    if not archive.is_file():
                        errors.append(f'snapshot {number}: missing crate archive {name} {version}')
                    elif hashlib.sha256(archive.read_bytes()).hexdigest() != record.get('cksum'):
                        errors.append(f'snapshot {number}: checksum mismatch for {name} {version}')
                    records.append(record)
                    previous = previous_entries.get((name, version))
                    if previous is not None:
                        before, after = dict(previous), dict(record)
                        before_yanked, after_yanked = before.pop('yanked', None), after.pop('yanked', None)
                        if before != after:
                            errors.append(f'snapshot {number}: immutable index entry changed for {name} {version}')
                        if before_yanked != after_yanked and not isinstance(after_yanked, bool):
                            errors.append(f'snapshot {number}: invalid yanked edit for {name} {version}')
                    previous_entries[(name, version)] = record
            except (OSError, json.JSONDecodeError, ValueError, TypeError) as error:
                errors.append(f'snapshot {number}: invalid index file {file}: {error}')

        indexed = {(record.get('name'), record.get('vers')) for record in records}
        port_indexed = set()
        for name, releases in ports.items():
            if not isinstance(name, str) or not isinstance(releases, list):
                errors.append(f'snapshot {number}: invalid ports.json crate entry {name!r}')
                continue
            for release in releases:
                if not isinstance(release, dict):
                    errors.append(f'snapshot {number}: invalid ports.json release for {name}')
                    continue
                version = release.get('version')
                if (not isinstance(version, str) or not isinstance(release.get('source_commit'), str)
                        or not isinstance(release.get('revision'), int)):
                    errors.append(f'snapshot {number}: invalid ports.json release metadata for {name}')
                    continue
                try:
                    upstream, revision = _version_parts(version)
                    if upstream != release.get('upstream_version') or revision != release.get('revision'):
                        errors.append(f'snapshot {number}: ports.json version fields disagree for {name} {version}')
                except ValueError as error:
                    errors.append(f'snapshot {number}: {error}')
                if (name, version) not in indexed:
                    errors.append(f'snapshot {number}: ports.json release missing index row {name} {version}')
                port_indexed.add((name, version))
        if indexed != port_indexed:
            errors.append(f'snapshot {number}: ports.json and index entries disagree')
        snapshot_records[number] = records

    if latest in snapshot_records:
        try:
            expected_page = render_page(root, latest)
            if (root / 'index.html').read_text() != expected_page:
                errors.append('index.html does not match latest snapshot')
        except (OSError, ValueError, json.JSONDecodeError) as error:
            errors.append(f'invalid index.html source data: {error}')

    if base_ref:
        errors.extend(_check_base_immutability(root, base_ref, latest))
    return errors


def _check_base_immutability(root: Path, base_ref: str, latest: int) -> list[str]:
    errors = []
    try:
        output = subprocess.run(
            ['git', 'diff', '--name-only', '-z', base_ref], cwd=root,
            check=True, capture_output=True,
        ).stdout
        changed = [Path(item.decode()) for item in output.split(b'\0') if item]
        for path in changed:
            match = re.match(r'index/(\d+)/(.+)', path.as_posix())
            if match and int(match.group(1)) <= latest:
                old = subprocess.run(['git', 'show', f'{base_ref}:{path.as_posix()}'], cwd=root,
                                     check=False, capture_output=True)
                new_path = root / path
                if old.returncode != 0 or not new_path.is_file():
                    errors.append(f'published snapshot changed: {path.as_posix()}')
                    continue
                try:
                    old_rows = [json.loads(line) for line in old.stdout.splitlines()]
                    new_rows = [json.loads(line) for line in new_path.read_bytes().splitlines()]
                    def without_yanked(row):
                        result = dict(row)
                        result.pop('yanked', None)
                        return result
                    if ([without_yanked(row) for row in old_rows]
                            != [without_yanked(row) for row in new_rows]):
                        errors.append(f'published snapshot changed: {path.as_posix()}')
                except (json.JSONDecodeError, TypeError):
                    errors.append(f'published snapshot is malformed: {path.as_posix()}')
    except (OSError, subprocess.CalledProcessError) as error:
        errors.append(f'cannot compare base ref {base_ref}: {error}')
    return errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--base-ref')
    args = parser.parse_args()
    errors = check_repository(args.root, args.base_ref)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
