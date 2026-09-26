import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.publish import publish


class SparseHttpTests(unittest.TestCase):
    def test_cargo_resolves_downloads_and_builds_from_successive_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'registry'
            app = Path(temporary) / 'app'
            archives = Path(temporary) / 'archives'
            app.mkdir()
            archives.mkdir()
            versions = ('1.0.0+yurt.1', '1.0.0+yurt.2')
            for version in versions:
                self._package(archives, version)
            publish(root, archives / f'probe-{versions[0]}.crate', 'a' * 40, 0)
            publish(root, archives / f'probe-{versions[1]}.crate', 'b' * 40, 1)

            class Handler(SimpleHTTPRequestHandler):
                def __init__(self, *args, **kwargs):
                    super().__init__(*args, directory=str(root), **kwargs)

                def log_message(self, _format, *_args):
                    pass

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join, 5)
            self.addCleanup(server.shutdown)
            registry_root = f'http://127.0.0.1:{server.server_port}'
            for snapshot, version in ((1, versions[0]), (2, versions[1])):
                config_path = root / 'index' / str(snapshot) / 'config.json'
                config_path.write_text(json.dumps({
                    'dl': f'{registry_root}/crates/{{crate}}/{{crate}}-{{version}}.crate',
                }) + '\n')
                manifest = app / 'Cargo.toml'
                manifest.write_text(
                    '[package]\nname = "consumer"\nversion = "0.1.0"\nedition = "2021"\n\n'
                    '[dependencies]\nprobe = "1"\n'
                )
                (app / 'src').mkdir(exist_ok=True)
                (app / 'src/main.rs').write_text('fn main() { probe::probe(); }\n')
                (app / 'Cargo.lock').unlink(missing_ok=True)
                index = f'sparse+{registry_root}/index/{snapshot}/'
                alias = 'probe_yurt'
                config = [
                    f'registries.yurt.index="{index}"',
                    f'patch.crates-io.{alias}.package="probe"',
                    f'patch.crates-io.{alias}.version="={version}"',
                    f'patch.crates-io.{alias}.registry="yurt"',
                ]
                common = ['cargo', '+1.98.1']
                for value in config:
                    common += ['--config', value]
                metadata = subprocess.run(
                    common + ['metadata', '--format-version=1', '--manifest-path', str(manifest)],
                    check=True, capture_output=True, text=True, timeout=90,
                )
                packages = json.loads(metadata.stdout)['packages']
                patched = next(package for package in packages if package['name'] == 'probe')
                self.assertEqual(patched['version'], version)
                self.assertEqual(patched['source'], f'sparse+{registry_root}/index/{snapshot}/')
                subprocess.run(
                    common + ['build', '--manifest-path', str(manifest)],
                    check=True, capture_output=True, text=True, timeout=90,
                )

    @staticmethod
    def _package(output: Path, version: str) -> None:
        source = output / f'probe-src-{version.replace("+", "-")}'
        (source / 'src').mkdir(parents=True)
        (source / 'Cargo.toml').write_text(
            '[package]\nname = "probe"\nversion = "' + version + '"\nedition = "2021"\n'
        )
        (source / 'src/lib.rs').write_text('pub fn probe() {}\n')
        subprocess.run(
            ['cargo', '+1.98.1', 'package', '--offline', '--allow-dirty',
             '--manifest-path', str(source / 'Cargo.toml'), '--target-dir', str(output / 'target')],
            check=True, capture_output=True, text=True, timeout=90,
        )
        archive = output / 'target/package' / f'probe-{version}.crate'
        shutil.copyfile(archive, output / archive.name)


if __name__ == '__main__':
    unittest.main()
