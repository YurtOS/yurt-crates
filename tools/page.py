"""Render a small human-readable listing for the latest registry snapshot."""
import html
import json
from pathlib import Path
import re


def render_page(root: Path, snapshot: int) -> str:
    if not isinstance(snapshot, int) or snapshot < 1:
        raise ValueError('snapshot must be a positive integer')
    data = json.loads((Path(root) / 'index' / str(snapshot) / 'ports.json').read_text())
    if not isinstance(data, dict):
        raise ValueError('ports.json must be an object')
    rows = []
    for name, releases in sorted(data.items()):
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name) or not isinstance(releases, list):
            raise ValueError(f'invalid ports.json crate entry: {name!r}')
        if any(not isinstance(release, dict) for release in releases):
            raise ValueError(f'invalid release entry for {name}')
        for release in sorted(releases, key=lambda item: item.get('upstream_version', '')):
            upstream = release.get('upstream_version')
            revision = release.get('revision')
            commit = release.get('source_commit')
            if (not isinstance(upstream, str) or not re.fullmatch(r'\d+\.\d+\.\d+', upstream)
                    or not isinstance(revision, int) or isinstance(revision, bool) or revision < 0
                    or not isinstance(commit, str)
                    or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit)):
                raise ValueError(f'invalid release metadata for {name}')
            rows.append(
                '<tr><td>' + html.escape(name) + '</td><td>' + html.escape(upstream)
                + '</td><td>' + str(revision) + '</td><td><code>' + html.escape(commit)
                + '</code></td></tr>'
            )
    return ('<!doctype html><html lang="en"><meta charset="utf-8"><title>Yurt crates</title>'
            '<h1>Yurt crates</h1><p>Snapshot ' + str(snapshot) + '</p>'
            '<table><thead><tr><th>Crate</th><th>Upstream version</th><th>Revision</th>'
            '<th>yurt-ports commit</th></tr></thead><tbody>' + ''.join(rows)
            + '</tbody></table></html>\n')
