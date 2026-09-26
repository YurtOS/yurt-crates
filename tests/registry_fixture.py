import io
from pathlib import Path
import tarfile


def crate_archive(directory: Path, version: str, *, name='rustix', marker='') -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{name}-{version}.crate'
    root = f'{name}-{version}'
    manifest = f'''[package]\nname = "{name}"\nversion = "{version}"\n{marker}\n[dependencies]\nlibc = "0.2"\n'''.encode()
    with tarfile.open(path, 'w:gz') as archive:
        info = tarfile.TarInfo(f'{root}/Cargo.toml')
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
    return path
