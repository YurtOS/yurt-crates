from pathlib import Path
import sys
import tempfile
import unittest
import json
import os
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from registry_fixture import crate_archive
from tools.check import check_repository
from tools.publish import publish


class CheckTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / 'registry'
        incoming = Path(temporary.name) / 'incoming'
        crate = crate_archive(incoming, '1.1.5+yurt.1')
        publish(root, crate, 'a' * 40, 0)
        crate2 = crate_archive(incoming, '1.1.5+yurt.2')
        publish(root, crate2, 'b' * 40, 1)
        return root

    def test_clean_history_passes(self):
        self.assertEqual(check_repository(self.fixture(), None), [])

    def test_corrupt_archive_reports_checksum(self):
        root = self.fixture()
        archive = root / 'crates/rustix/rustix-1.1.5+yurt.1.crate'
        archive.write_bytes(archive.read_bytes() + b'corrupt')
        self.assertTrue(any('checksum' in error for error in check_repository(root, None)))

    def test_missing_dependency_registry_reports_violation(self):
        root = self.fixture()
        path = root / 'index/2/ru/st/rustix'
        import json
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]['deps'][0].pop('registry')
        path.write_text(''.join(json.dumps(row, sort_keys=True, separators=(',', ':')) + '\n' for row in rows))
        self.assertTrue(any('registry' in error for error in check_repository(root, None)))

    def test_ports_json_disagreement_reports_violation(self):
        root = self.fixture()
        path = root / 'index/2/ports.json'
        data = json.loads(path.read_text())
        data['rustix'][0]['revision'] = 99
        path.write_text(json.dumps(data))
        self.assertTrue(any('ports.json' in error for error in check_repository(root, None)))

    def test_duplicate_versions_ignoring_build_metadata_reports_violation(self):
        root = self.fixture()
        path = root / 'index/2/ru/st/rustix'
        row = json.loads(path.read_text())
        row['vers'] = '1.1.5+yurt.3'
        with path.open('a') as stream:
            stream.write(json.dumps(row) + '\n')
        self.assertTrue(any('duplicate upstream version' in error
                            for error in check_repository(root, None)))

    def test_old_snapshot_entry_mutation_is_reported(self):
        root = self.fixture()
        subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
        subprocess.run(['git', 'add', '.'], cwd=root, check=True)
        environment = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                          GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        subprocess.run(['git', 'commit', '-qm', 'baseline'], cwd=root, env=environment, check=True)
        base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()
        path = root / 'index/1/ru/st/rustix'
        row = json.loads(path.read_text())
        row['features']['injected'] = []
        path.write_text(json.dumps(row) + '\n')
        self.assertTrue(any('published snapshot changed' in error
                            for error in check_repository(root, base)))

    def test_page_escapes_listing_values_and_matches_snapshot(self):
        root = self.fixture()
        page = (root / 'index.html').read_text()
        self.assertIn('rustix', page)
        self.assertIn('1.1.5', page)
        self.assertIn('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', page)


if __name__ == '__main__':
    unittest.main()
