"""Create immutable sparse-index snapshots from verified Cargo archives."""
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import argparse
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.index import index_entry, index_path, read_crate
from tools.page import render_page

DOWNLOAD = 'https://yurtos.github.io/yurt-crates/crates/{crate}/{crate}-{version}.crate'


def publish(root: Path, crate_path: Path, ports_commit: str, expected_latest: int) -> int:
    root, crate_path = Path(root), Path(crate_path)
    if not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', ports_commit):
        raise ValueError('ports_commit must be a full hexadecimal commit id')
    if not isinstance(expected_latest, int) or expected_latest < 0:
        raise ValueError('expected_latest must be a nonnegative integer')
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.publish.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = _latest(root)
        if current != expected_latest:
            raise ValueError(f'stale latest: expected {expected_latest}, found {current}')
        crate = read_crate(crate_path)
        upstream, revision = _version_parts(crate.version)
        snapshot = current + 1
        if (root / f'index/{snapshot}').exists():
            raise ValueError(f'snapshot {snapshot} already exists')
        crate_dir = root / 'crates' / crate.name
        destination = crate_dir / f'{crate.name}-{crate.version}.crate'
        if destination.exists():
            raise ValueError(f'crate archive already exists: {destination.name}')

        previous = root / 'index' / str(current) if current else None
        previous_rows = _load_ports(previous / 'ports.json') if previous else {}
        entries = previous_rows.setdefault(crate.name, [])
        matching = [row for row in entries if row['upstream_version'] == upstream]
        if matching and revision <= max(row['revision'] for row in matching):
            raise ValueError(f'{crate.name} {upstream}: revision {revision} is not higher')
        new_rows = [row for row in entries if row['upstream_version'] != upstream]
        new_rows.append({
            'upstream_version': upstream, 'revision': revision,
            'version': crate.version, 'source_commit': ports_commit,
        })
        previous_rows[crate.name] = sorted(new_rows, key=lambda row: row['upstream_version'])

        stage_parent = root / '.staging'
        stage_parent.mkdir(exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f'{snapshot}-', dir=stage_parent))
        moved_crate = False
        temporary_crate = None
        previous_page = (root / 'index.html').read_bytes() if (root / 'index.html').exists() else None
        try:
            if previous:
                shutil.copytree(previous, stage, dirs_exist_ok=True)
            index_file = stage / index_path(crate.name)
            old_records = _read_index(index_file) if index_file.exists() else []
            kept = [record for record in old_records if _version_parts(record['vers'])[0] != upstream]
            kept.append(index_entry(crate))
            kept.sort(key=lambda record: record['vers'])
            index_file.parent.mkdir(parents=True, exist_ok=True)
            index_file.write_text(''.join(json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n'
                                           for record in kept))
            (stage / 'config.json').write_text(json.dumps({'dl': DOWNLOAD}, sort_keys=True) + '\n')
            (stage / 'ports.json').write_text(json.dumps(previous_rows, sort_keys=True, indent=2) + '\n')
            _validate_snapshot(stage, root, crate, destination)

            page = _render_staged_page(stage, snapshot)
            with tempfile.TemporaryDirectory(prefix='yurt-crates-check-') as candidate_name:
                candidate = Path(candidate_name)
                shutil.copytree(root, candidate, dirs_exist_ok=True, ignore=shutil.ignore_patterns(
                    '.git', '.staging', '.publish.lock', 'latest.tmp', 'index.html.tmp',
                ))
                (candidate / 'crates' / crate.name).mkdir(parents=True, exist_ok=True)
                shutil.copyfile(crate_path, candidate / 'crates' / crate.name / destination.name)
                shutil.copytree(stage, candidate / 'index' / str(snapshot))
                (candidate / 'index.html').write_text(page)
                (candidate / 'latest').write_text(f'{snapshot}\n')
                from tools.check import check_repository
                errors = check_repository(candidate, None)
                if errors:
                    raise ValueError('prospective registry failed consistency check: ' + '; '.join(errors))

            crate_dir.mkdir(parents=True, exist_ok=True)
            temporary_crate = crate_dir / f'.{destination.name}.tmp'
            shutil.copyfile(crate_path, temporary_crate)
            os.replace(temporary_crate, destination)
            moved_crate = True
            final_snapshot = root / 'index' / str(snapshot)
            final_snapshot.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, final_snapshot)
            (root / 'index.html.tmp').write_text(page)
            os.replace(root / 'index.html.tmp', root / 'index.html')
            (root / 'latest.tmp').write_text(f'{snapshot}\n')
            os.replace(root / 'latest.tmp', root / 'latest')
            return snapshot
        except Exception:
            if temporary_crate is not None:
                temporary_crate.unlink(missing_ok=True)
            if moved_crate:
                destination.unlink(missing_ok=True)
            shutil.rmtree(root / 'index' / str(snapshot), ignore_errors=True)
            (root / 'latest.tmp').unlink(missing_ok=True)
            (root / 'index.html.tmp').unlink(missing_ok=True)
            if previous_page is None:
                (root / 'index.html').unlink(missing_ok=True)
            else:
                (root / 'index.html').write_bytes(previous_page)
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)


def _latest(root: Path) -> int:
    path = root / 'latest'
    if not path.exists():
        return 0
    value = path.read_text().strip()
    if not value.isdecimal():
        raise ValueError('latest must contain a snapshot number')
    return int(value)


def _version_parts(version: str) -> tuple[str, int]:
    match = re.fullmatch(r'(\d+\.\d+\.\d+)\+yurt\.(\d+)', version)
    if not match:
        raise ValueError(f'ported crate version must be X.Y.Z+yurt.N: {version}')
    return match.group(1), int(match.group(2))


def _load_ports(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError('ports.json must be an object')
    return data


def _read_index(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _render_staged_page(stage: Path, snapshot: int) -> str:
    with tempfile.TemporaryDirectory(prefix='yurt-crates-page-') as temporary:
        root = Path(temporary)
        (root / 'index').mkdir()
        shutil.copytree(stage, root / 'index' / str(snapshot))
        return render_page(root, snapshot)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--crate', type=Path, required=True)
    parser.add_argument('--ports-commit', required=True)
    parser.add_argument('--expected-latest', type=int, required=True)
    args = parser.parse_args()
    try:
        number = publish(args.root, args.crate, args.ports_commit, args.expected_latest)
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    print(number)
    return 0


def _validate_snapshot(stage: Path, root: Path, new_crate, destination: Path) -> None:
    config = json.loads((stage / 'config.json').read_text())
    if config != {'dl': DOWNLOAD}:
        raise ValueError('invalid sparse index config')
    rows = _load_ports(stage / 'ports.json')
    records = []
    for index_file in stage.rglob('*'):
        if index_file.is_file() and index_file.name not in {'config.json', 'ports.json'}:
            records.extend(_read_index(index_file))
    seen = set()
    for record in records:
        name, version = record.get('name'), record.get('vers')
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError('index record missing name/version')
        upstream, _ = _version_parts(version)
        identity = (name.lower(), upstream)
        if identity in seen:
            raise ValueError(f'duplicate upstream version in snapshot: {identity}')
        seen.add(identity)
        if not all(isinstance(dep, dict) and isinstance(dep.get('registry'), str)
                   for dep in record.get('deps', [])):
            raise ValueError(f'{name} {version} dependency missing registry')
        archive = destination if (name, version) == (new_crate.name, new_crate.version) \
            else root / 'crates' / name / f'{name}-{version}.crate'
        if archive == destination:
            expected = new_crate.checksum
        else:
            if not archive.is_file():
                raise ValueError(f'missing crate archive: {archive}')
            expected = __import__('hashlib').sha256(archive.read_bytes()).hexdigest()
        if record.get('cksum') != expected:
            raise ValueError(f'checksum mismatch for {name} {version}')
    for name, releases in rows.items():
        for release in releases:
            if (name, release['version']) not in {
                (record['name'], record['vers']) for record in records
            }:
                raise ValueError(f'ports.json entry missing from index: {name} {release["version"]}')


if __name__ == '__main__':
    raise SystemExit(main())
