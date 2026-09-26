from pathlib import Path
import sys
import tempfile
import unittest
import json
import os
import shutil
import subprocess
import re
import textwrap

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from registry_fixture import crate_archive
from tools.check import check_repository
from tools.page import render_page
from tools.publish import publish


class CheckTests(unittest.TestCase):
    def test_empty_registry_is_valid_before_first_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(check_repository(Path(temporary), None), [])

    def test_pages_deployment_serializes_and_rejects_stale_checked_sha(self):
        workflows = Path(__file__).resolve().parents[1] / '.github' / 'workflows'
        pages = (workflows / 'pages.yml').read_text()
        publish_workflow = (workflows / 'publish-crate.yml').read_text()
        self.assertIn('workflow_dispatch:', pages)
        self.assertIn('github.event_name == \'workflow_dispatch\'', pages)
        self.assertIn('ref: ${{ github.event.workflow_run.head_sha || github.sha }}', pages)
        self.assertIn('group: yurt-crates-deployment', pages)
        self.assertIn('group: yurt-crates-deployment', publish_workflow)
        self.assertIn('queue: max', pages)
        self.assertIn('queue: max', publish_workflow)
        self.assertIn('CHECKED_SHA: ${{ github.event.workflow_run.head_sha || github.sha }}', pages)
        self.assertIn('if [[ "$(git rev-parse origin/main)" != "$CHECKED_SHA" ]]; then', pages)
        self.assertIn("if: steps.fresh.outputs.deploy == 'true'", pages)

    def test_pages_validation_blocks_invalid_committed_snapshots(self):
        repository = Path(__file__).resolve().parents[1]
        pages = (repository / '.github/workflows/pages.yml').read_text()
        match = re.search(
            r'      - name: Validate registry\n(.*?)        run: \|\n((?:          [^\n]*\n)+)',
            pages, re.DOTALL,
        )
        self.assertIsNotNone(match, 'Pages must validate its checkout before staging')
        self.assertLess(match.start(), pages.index('      - name: Stage static registry'))
        self.assertIn("if: steps.fresh.outputs.deploy == 'true'", match.group(1))
        script = textwrap.dedent(match.group(2))
        for case, diagnostic in (
            ('valid', None),
            ('corrupt', 'checksum mismatch'),
            ('deleted', 'published registry data was removed'),
            ('rollback', 'latest moved backwards'),
        ):
            with self.subTest(case=case):
                root, _ = self._committed_baseline()
                shutil.copytree(repository / 'tools', root / 'tools',
                                ignore=shutil.ignore_patterns('__pycache__'))
                if case == 'corrupt':
                    archive = root / 'crates/rustix/rustix-1.1.5+yurt.1.crate'
                    archive.write_bytes(archive.read_bytes() + b'corrupt')
                elif case == 'deleted':
                    for name in ('latest', 'index.html'):
                        (root / name).unlink()
                    for name in ('index', 'crates'):
                        shutil.rmtree(root / name)
                elif case == 'rollback':
                    (root / 'latest').write_text('1\n')
                    (root / 'index.html').write_text(render_page(root, 1))
                subprocess.run(['git', 'add', '-A'], cwd=root, check=True)
                subprocess.run(['git', '-c', 'user.name=Test', '-c',
                                'user.email=test@example.invalid', 'commit', '-qm', 'candidate'],
                               cwd=root, check=True)
                result = subprocess.run(['bash', '-e', '-c', script + '\necho stage-ready'],
                                        cwd=root, capture_output=True, text=True)
                if diagnostic is None:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn('stage-ready', result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(diagnostic, result.stderr)
                    self.assertNotIn('stage-ready', result.stdout)

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

    def test_index_dependency_metadata_must_match_crate_archive(self):
        root = self.fixture()
        path = root / 'index/2/ru/st/rustix'
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        rows[0]['deps'] = []
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        self.assertTrue(any('metadata differs from crate archive' in error
                            for error in check_repository(root, None)))

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

    def test_deleting_all_published_files_is_rejected_against_base(self):
        root, base = self._committed_baseline()
        for name in ('latest', 'index.html'):
            (root / name).unlink()
        for name in ('index', 'crates'):
            shutil.rmtree(root / name)
        self.assertTrue(check_repository(root, base))

    def test_latest_cannot_move_backwards_from_base(self):
        root, base = self._committed_baseline()
        (root / 'latest').write_text('1\n')
        (root / 'index.html').write_text(render_page(root, 1))
        self.assertTrue(any('latest moved backwards' in error
                            for error in check_repository(root, base)))

    def _committed_baseline(self):
        root = self.fixture()
        subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
        subprocess.run(['git', 'add', '.'], cwd=root, check=True)
        environment = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                          GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        subprocess.run(['git', 'commit', '-qm', 'baseline'], cwd=root, env=environment, check=True)
        base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()
        return root, base

    def test_new_snapshot_compares_cleanly_against_existing_base(self):
        root = self.fixture()
        subprocess.run(['git', 'init', '-q'], cwd=root, check=True)
        subprocess.run(['git', 'add', '.'], cwd=root, check=True)
        environment = dict(os.environ, GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                          GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        subprocess.run(['git', 'commit', '-qm', 'baseline'], cwd=root, env=environment, check=True)
        base = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()
        incoming = root.parent / 'incoming'
        archive = crate_archive(incoming, '1.1.6+yurt.1')
        publish(root, archive, 'c' * 40, 2)
        self.assertEqual(check_repository(root, base), [])

    def test_yanked_only_edit_to_existing_snapshot_is_allowed(self):
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
        row['yanked'] = True
        path.write_text(json.dumps(row) + '\n')
        self.assertEqual(check_repository(root, base), [])

    def test_page_escapes_listing_values_and_matches_snapshot(self):
        root = self.fixture()
        page = (root / 'index.html').read_text()
        self.assertIn('rustix', page)
        self.assertIn('1.1.5', page)
        self.assertIn('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', page)


if __name__ == '__main__':
    unittest.main()
