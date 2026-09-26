"""Read Cargo crate archives and render standard sparse-index entries."""
from dataclasses import dataclass
import hashlib
import io
from pathlib import Path, PurePosixPath
import re
import tarfile
import tomllib
from urllib.parse import urlsplit


CRATES_IO_INDEX = 'https://github.com/rust-lang/crates.io-index'
CRATES_IO_INDEX_URLS = {
    CRATES_IO_INDEX,
    'https://index.crates.io/',
    'sparse+https://index.crates.io/',
}
DEPENDENCY_KINDS = (
    ('dependencies', 'normal'),
    ('build-dependencies', 'build'),
    ('dev-dependencies', 'dev'),
)
DEPENDENCY_FIELDS = {
    'version', 'package', 'registry-index', 'features', 'optional', 'default-features',
}
CRATE_NAME = re.compile(r'[A-Za-z0-9_-]+\Z', re.ASCII)


@dataclass(frozen=True)
class CrateMetadata:
    name: str
    version: str
    checksum: str
    dependencies: tuple[dict, ...]
    features: dict[str, list[str]]
    links: str | None
    rust_version: str | None
    extended_features: bool


def index_path(name: str) -> Path:
    """Return Cargo's lowercase sparse-index path for a crate name."""
    if not isinstance(name, str) or not CRATE_NAME.fullmatch(name):
        raise ValueError(f'invalid crate name: {name!r}')
    lower = name.lower()
    if len(lower) == 1:
        return Path('1') / lower
    if len(lower) == 2:
        return Path('2') / lower
    if len(lower) == 3:
        return Path('3') / lower[0] / lower
    return Path(lower[:2]) / lower[2:4] / lower


def read_crate(path: Path) -> CrateMetadata:
    """Read the normalized Cargo manifest from a `.crate` and hash its bytes."""
    path = Path(path)
    try:
        archive_bytes = path.read_bytes()
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode='r:*') as archive:
            members = archive.getmembers()
            root = _crate_root(members)
            manifest_member = next(
                (member for member in members if member.name == f'{root}/Cargo.toml'), None
            )
            if manifest_member is None or not manifest_member.isfile():
                raise ValueError('crate archive is missing its root Cargo.toml')
            stream = archive.extractfile(manifest_member)
            if stream is None:
                raise ValueError('crate archive Cargo.toml is not a regular file')
            manifest = tomllib.loads(stream.read().decode('utf-8'))
    except (OSError, tarfile.TarError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f'cannot read Cargo crate {path}: {error}') from error

    package = manifest.get('package')
    if not isinstance(package, dict):
        raise ValueError('Cargo manifest is missing its [package] table')
    name = package.get('name')
    version = package.get('version')
    if not isinstance(name, str) or not CRATE_NAME.fullmatch(name):
        raise ValueError(f'invalid package name in Cargo manifest: {name!r}')
    if not isinstance(version, str) or not version:
        raise ValueError(f'invalid package version in Cargo manifest: {version!r}')
    if root != f'{name}-{version}':
        raise ValueError(f'crate root {root!r} does not match package {name}-{version}')

    features = _read_features(manifest.get('features', {}))
    dependencies = _read_dependencies(manifest)
    links = package.get('links')
    rust_version = package.get('rust-version')
    if links is not None and not isinstance(links, str):
        raise ValueError('package.links must be a string')
    if rust_version is not None and not isinstance(rust_version, str):
        raise ValueError('package.rust-version must be a string')
    extended_features = any(
        'dep:' in value or '?/' in value
        for values in features.values() for value in values
    )

    return CrateMetadata(
        name=name,
        version=version,
        checksum=hashlib.sha256(archive_bytes).hexdigest(),
        dependencies=tuple(dependencies),
        features=features,
        links=links,
        rust_version=rust_version,
        extended_features=extended_features,
    )


def index_entry(crate: CrateMetadata) -> dict:
    """Convert crate metadata to one Cargo sparse-index JSON record."""
    entry = {
        'name': crate.name,
        'vers': crate.version,
        'deps': [dict(dependency) for dependency in crate.dependencies],
        'cksum': crate.checksum,
        'features': {name: list(values) for name, values in crate.features.items()},
        'yanked': False,
    }
    if crate.links is not None:
        entry['links'] = crate.links
    if crate.extended_features:
        entry['v'] = 2
    if crate.rust_version is not None:
        entry['rust_version'] = crate.rust_version
    return entry


def _crate_root(members: list[tarfile.TarInfo]) -> str:
    if not members:
        raise ValueError('crate archive is empty')
    roots = set()
    seen = set()
    for member in members:
        path = PurePosixPath(member.name)
        if (path.is_absolute() or '..' in path.parts or not path.parts
                or member.issym() or member.islnk()
                or not (member.isfile() or member.isdir())):
            raise ValueError(f'unsafe archive member: {member.name!r}')
        if member.name in seen:
            raise ValueError(f'duplicate archive member: {member.name!r}')
        seen.add(member.name)
        roots.add(path.parts[0])
    if len(roots) != 1:
        raise ValueError(f'crate archive has multiple roots: {sorted(roots)!r}')
    return roots.pop()


def _read_features(value) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise ValueError('Cargo features must be a table')
    features = {}
    for name, members in value.items():
        if not isinstance(name, str) or not isinstance(members, list) \
                or any(not isinstance(member, str) for member in members):
            raise ValueError(f'invalid Cargo feature entry: {name!r}')
        features[name] = list(members)
    return features


def _read_dependencies(manifest: dict) -> list[dict]:
    dependencies = []
    for table_name, kind in DEPENDENCY_KINDS:
        _append_dependencies(
            dependencies, manifest.get(table_name, {}), kind=kind, target=None,
        )

    targets = manifest.get('target', {})
    if not isinstance(targets, dict):
        raise ValueError('Cargo target dependencies must be a table')
    for target, target_tables in targets.items():
        if not isinstance(target, str) or not isinstance(target_tables, dict):
            raise ValueError(f'invalid Cargo target dependency table: {target!r}')
        known_tables = {table_name for table_name, _ in DEPENDENCY_KINDS}
        unknown_tables = set(target_tables) - known_tables
        if unknown_tables:
            raise ValueError(f'unknown target dependency tables: {sorted(unknown_tables)!r}')
        for table_name, kind in DEPENDENCY_KINDS:
            _append_dependencies(
                dependencies, target_tables.get(table_name, {}), kind=kind, target=target,
            )

    dependencies.sort(key=lambda dependency: (
        dependency['name'], dependency['target'] or '', dependency['kind'],
        dependency['package'] or '', dependency['req'],
    ))
    return dependencies


def _append_dependencies(output: list[dict], table, *, kind: str, target: str | None) -> None:
    if not isinstance(table, dict):
        raise ValueError(f'Cargo {kind} dependencies must be a table')
    for name, specification in table.items():
        if not isinstance(name, str) or not CRATE_NAME.fullmatch(name):
            raise ValueError(f'invalid dependency name: {name!r}')
        if isinstance(specification, str):
            specification = {'version': specification}
        if not isinstance(specification, dict):
            raise ValueError(f'invalid dependency specification for {name!r}')
        unknown = set(specification) - DEPENDENCY_FIELDS
        if unknown:
            raise ValueError(f'unknown dependency field(s) for {name}: {sorted(unknown)!r}')

        requirement = specification.get('version')
        if not isinstance(requirement, str) or not requirement:
            raise ValueError(f'dependency {name!r} has no version requirement')
        package = specification.get('package')
        if package is not None and (
            not isinstance(package, str) or not CRATE_NAME.fullmatch(package)
        ):
            raise ValueError(f'invalid package name for dependency {name!r}')
        registry = specification.get('registry-index', CRATES_IO_INDEX)
        if not isinstance(registry, str) or not registry:
            raise ValueError(f'invalid registry-index for dependency {name!r}')
        if registry in CRATES_IO_INDEX_URLS:
            registry = CRATES_IO_INDEX
        else:
            _require_registry_url(registry, name)

        features = specification.get('features', [])
        optional = specification.get('optional', False)
        default_features = specification.get('default-features', True)
        if not isinstance(features, list) or any(not isinstance(item, str) for item in features):
            raise ValueError(f'dependency features for {name!r} must be a string array')
        if not isinstance(optional, bool):
            raise ValueError(f'dependency optional flag for {name!r} must be boolean')
        if not isinstance(default_features, bool):
            raise ValueError(f'dependency default-features flag for {name!r} must be boolean')

        output.append({
            'name': name,
            'package': package,
            'req': requirement,
            'features': list(features),
            'optional': optional,
            'default_features': default_features,
            'target': target,
            'kind': kind,
            'registry': registry,
        })


def _require_registry_url(registry: str, dependency: str) -> None:
    url = registry.removeprefix('sparse+')
    try:
        parsed = urlsplit(url)
        invalid = (
            parsed.scheme not in {'http', 'https', 'file'}
            or (parsed.scheme in {'http', 'https'} and not parsed.netloc)
            or (parsed.scheme == 'file' and not parsed.path)
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment
        )
    except ValueError:
        invalid = True
    if invalid:
        raise ValueError(
            f'registry-index for dependency {dependency!r} must be a resolved registry URL'
        )
