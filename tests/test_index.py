import hashlib
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.index import index_entry, index_path, read_crate


MANIFEST = '''\
[package]
name = "fixture"
version = "1.2.3+yurt.4"
links = "fixture_native"
rust-version = "1.82"

[features]
default = ["dep:serde"]
extended = ["dep:serde", "platform_io?/std"]

[dependencies]
serde = { version = "1.0", features = ["derive"], optional = true, default-features = false }
renamed_io = { package = "rustix", version = "=1.1.5", features = ["fs"], default-features = false }
registry_client = { package = "alt-client", version = "^2", registry-index = "sparse+https://registry.example/index/", optional = true }

[build-dependencies]
cc = { version = "1.0", default-features = true }

[dev-dependencies]
tempfile = "3"

[target.'cfg(target_os = "linux")'.dependencies]
platform_io = { package = "rustix", version = "1.1.5", registry-index = "sparse+https://registry.example/index/", optional = true }
'''


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fixture(self, name='fixture-1.2.3+yurt.4.crate', manifest=MANIFEST,
                extra_members=()):
        archive_path = self.root / name
        root = 'fixture-1.2.3+yurt.4'
        with tarfile.open(archive_path, 'w:gz') as archive:
            contents = manifest.encode()
            info = tarfile.TarInfo(f'{root}/Cargo.toml')
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
            for member_name, member_type, linkname in extra_members:
                info = tarfile.TarInfo(member_name)
                info.type = member_type
                info.linkname = linkname
                archive.addfile(info)
        return archive_path

    def test_index_path_uses_cargo_lowercase_layout(self):
        self.assertEqual(index_path('x'), Path('1/x'))
        self.assertEqual(index_path('ab'), Path('2/ab'))
        self.assertEqual(index_path('abc'), Path('3/a/abc'))
        self.assertEqual(index_path('rustix'), Path('ru/st/rustix'))
        self.assertEqual(index_path('Rustix'), Path('ru/st/rustix'))

    def test_entry_preserves_package_and_dependency_metadata(self):
        path = self.fixture()
        crate = read_crate(path)
        entry = index_entry(crate)

        self.assertEqual(entry['name'], 'fixture')
        self.assertEqual(entry['vers'], '1.2.3+yurt.4')
        self.assertEqual(entry['cksum'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(entry['features'], {
            'default': ['dep:serde'],
            'extended': ['dep:serde', 'platform_io?/std'],
        })
        self.assertEqual(entry['v'], 2)
        self.assertEqual(entry['yanked'], False)
        self.assertEqual(entry['links'], 'fixture_native')
        self.assertEqual(entry['rust_version'], '1.82')

        deps = {(dep['name'], dep['kind'], dep['target']): dep for dep in entry['deps']}
        self.assertEqual(set(deps), {
            ('serde', 'normal', None),
            ('renamed_io', 'normal', None),
            ('registry_client', 'normal', None),
            ('cc', 'build', None),
            ('tempfile', 'dev', None),
            ('platform_io', 'normal', 'cfg(target_os = "linux")'),
        })

        serde = deps[('serde', 'normal', None)]
        self.assertEqual(serde, {
            'name': 'serde', 'package': None, 'req': '1.0', 'features': ['derive'],
            'optional': True, 'default_features': False, 'target': None,
            'kind': 'normal', 'registry': 'https://github.com/rust-lang/crates.io-index',
        })
        renamed = deps[('renamed_io', 'normal', None)]
        self.assertEqual(renamed['package'], 'rustix')
        self.assertEqual(renamed['req'], '=1.1.5')
        self.assertFalse(renamed['default_features'])
        alternate = deps[('registry_client', 'normal', None)]
        self.assertEqual(alternate['package'], 'alt-client')
        self.assertEqual(alternate['registry'], 'sparse+https://registry.example/index/')
        self.assertTrue(alternate['optional'])
        self.assertEqual(deps[('cc', 'build', None)]['default_features'], True)
        self.assertEqual(deps[('tempfile', 'dev', None)]['req'], '3')
        platform = deps[('platform_io', 'normal', 'cfg(target_os = "linux")')]
        self.assertEqual(platform['package'], 'rustix')
        self.assertEqual(platform['registry'], 'sparse+https://registry.example/index/')

    def test_entry_omits_optional_schema_fields_without_source_values(self):
        manifest = '''\
[package]
name = "fixture"
version = "1.2.3+yurt.4"
'''
        entry = index_entry(read_crate(self.fixture(manifest=manifest)))
        self.assertEqual(entry['features'], {})
        self.assertNotIn('v', entry)
        self.assertNotIn('links', entry)
        self.assertNotIn('rust_version', entry)

    def test_read_crate_rejects_registry_alias_without_resolved_index_url(self):
        manifest = MANIFEST.replace(
            'registry-index = "sparse+https://registry.example/index/"',
            'registry-index = "alternate-registry"',
            1,
        )
        with self.assertRaisesRegex(ValueError, 'registry-index.*URL'):
            read_crate(self.fixture(manifest=manifest))

    def test_index_path_rejects_invalid_crate_names(self):
        for name in ('', '../rustix', 'has space', 'café'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                index_path(name)

    def test_read_crate_rejects_dependency_fields_it_cannot_encode(self):
        manifest = MANIFEST.replace(
            'version = "1.0", features = ["derive"], optional = true, default-features = false',
            'version = "1.0", features = ["derive"], optional = true, '
            'default-features = false, unknown-field = "must not be dropped"',
        )
        with self.assertRaisesRegex(ValueError, 'unknown dependency field'):
            read_crate(self.fixture(manifest=manifest))

    def test_read_crate_rejects_traversal_and_links(self):
        unsafe = self.fixture(extra_members=(
            ('fixture-1.2.3+yurt.4/../../outside', tarfile.REGTYPE, ''),
        ))
        with self.assertRaisesRegex(ValueError, 'unsafe archive member'):
            read_crate(unsafe)

        linked = self.fixture(name='linked.crate', extra_members=(
            ('fixture-1.2.3+yurt.4/link', tarfile.SYMTYPE, '/etc/passwd'),
        ))
        with self.assertRaisesRegex(ValueError, 'unsafe archive member'):
            read_crate(linked)


if __name__ == '__main__':
    unittest.main()
