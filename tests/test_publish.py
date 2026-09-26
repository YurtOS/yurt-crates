from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from registry_fixture import crate_archive
from tools.publish import publish


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'registry'
        self.incoming = Path(self.temp.name) / 'incoming'
        self.one = crate_archive(self.incoming, '1.1.5+yurt.1')
        self.two = crate_archive(self.incoming, '1.1.5+yurt.2')

    def test_snapshots_are_immutable_and_latest_moves_last(self):
        self.assertEqual(publish(self.root, self.one, 'a' * 40, 0), 1)
        old_index = (self.root / 'index/1/ru/st/rustix').read_bytes()
        self.assertEqual(publish(self.root, self.two, 'b' * 40, 1), 2)
        self.assertEqual((self.root / 'index/1/ru/st/rustix').read_bytes(), old_index)
        new_lines = (self.root / 'index/2/ru/st/rustix').read_text().splitlines()
        self.assertEqual([__import__('json').loads(line)['vers'] for line in new_lines],
                         ['1.1.5+yurt.2'])
        self.assertTrue((self.root / 'crates/rustix/rustix-1.1.5+yurt.1.crate').is_file())
        self.assertTrue((self.root / 'crates/rustix/rustix-1.1.5+yurt.2.crate').is_file())
        self.assertEqual((self.root / 'latest').read_text(), '2\n')

    def test_duplicate_nonmonotonic_and_stale_publish_fail_without_changes(self):
        publish(self.root, self.one, 'a' * 40, 0)
        before = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with self.assertRaises(ValueError):
            publish(self.root, self.one, 'a' * 40, 1)
        with self.assertRaises(ValueError):
            publish(self.root, self.two, 'b' * 40, 0)
        zero = crate_archive(self.incoming, '1.1.5+yurt.0')
        with self.assertRaises(ValueError):
            publish(self.root, zero, 'c' * 40, 1)
        after = {p.relative_to(self.root): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(after, before)

    def test_render_failure_does_not_advance_latest_or_leave_partial_files(self):
        with patch('tools.publish.render_page', side_effect=RuntimeError('injected render failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                publish(self.root, self.one, 'a' * 40, 0)
        self.assertFalse((self.root / 'latest').exists())
        self.assertFalse((self.root / 'index/1').exists())
        self.assertFalse((self.root / 'crates/rustix/rustix-1.1.5+yurt.1.crate').exists())

    def test_command_line_publisher_runs_the_publication_path(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[1] / 'tools/publish.py'),
             '--root', str(self.root), '--crate', str(self.one), '--ports-commit', 'a' * 40,
             '--expected-latest', '0'],
            check=True, capture_output=True, text=True,
        )
        self.assertEqual(result.stdout.strip(), '1')
        self.assertEqual((self.root / 'latest').read_text(), '1\n')


if __name__ == '__main__':
    unittest.main()
