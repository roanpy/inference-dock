#!/usr/bin/env python3
"""Export a reviewed allowlist without local configuration or Git history."""
import argparse
import gzip
import hashlib
import re
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    'LICENSE', 'README.md', 'README.zh-CN.md', 'CHANGELOG.md', 'SECURITY.md',
    'CONTRIBUTING.md', 'THIRD_PARTY_NOTICES.md', 'RELEASING.md', 'requirements.txt', 'Package.swift',
    'assets/AppIcon.svg', 'assets/AppIcon.png', 'assets/AppIcon.icns',
    'config/engines.example.yaml', 'config/production.example.yaml',
    'scripts/model_dispatch.py', 'scripts/fake_backend.py', 'scripts/agent_config.py',
    'scripts/package_app.sh', 'scripts/plugin_registry.py', 'scripts/plugin_runtime.py',
    'scripts/update_monitor.py',
    'scripts/migration_helper.py', 'scripts/export_source.py', 'scripts/acceptance.sh',
    'docs/platforms.md', 'docs/migration.md', 'docs/plugin-authoring.md',
    'docs/implementation-design.md', 'docs/release-checklist.md', 'docs/release-notes.md',
    'docs/runtime-validation-2026-09-10.md',
    'docs/compatibility-matrix.md', 'docs/configuration.md', 'docs/troubleshooting.md',
    'docs/nightly-runbook.md', 'docs/acceptance-checklist.md',
    '.github/workflows/ci.yml',
]
TREES = ['apps', 'plugins', 'tests']

PRIVATE_PATH = re.compile(rb'(/Users/[A-Za-z0-9._-]+/|/private/var/[A-Za-z0-9._-]+/)')
SECRET_VALUE = re.compile(
    rb'(Bearer\s+[A-Za-z0-9._-]{16,}|sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})'
)
SECRET_ASSIGNMENT = re.compile(
    rb'(?im)^\s*(?:export\s+)?[A-Z][A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY)\s*[:=]\s*["\']?([^\s"\']{8,})'
)
MODEL_EXTENSIONS = {'.safetensors', '.bin'}
PRIVATE_ARTIFACT_SUFFIXES = {'.log', '.jsonl', '.sqlite', '.sqlite3', '.db'}
PRIVATE_MARKERS = (b'inference-dock' + b'-dev',)

def public_metadata(member):
    member.uid = member.gid = 0
    member.uname = member.gname = ''
    member.pax_headers = {}
    member.mtime = 0
    return member

def export(destination):
    paths = {ROOT / name for name in FILES}
    for directory in TREES:
        if not (ROOT / directory).is_dir():
            raise ValueError(f'Missing release directory: {directory}')
        paths.update(p for p in (ROOT / directory).rglob('*')
                     if p.is_file() and p.name != '.DS_Store' and '__pycache__' not in p.parts and p.suffix != '.pyc')
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'Invalid release member: {path.relative_to(ROOT)}')
        relative = path.relative_to(ROOT)
        # Never ship generated logs/caches or heavyweight model payloads.
        if any(part in {'logs', 'cache', 'dist', '.build', '.venv'} for part in relative.parts):
            raise ValueError(f'Generated/private path in release: {relative}')
        if path.suffix.lower() in MODEL_EXTENSIONS:
            raise ValueError(f'Model artifact in release: {relative}')
        if path.suffix.lower() in PRIVATE_ARTIFACT_SUFFIXES:
            raise ValueError(f'Generated/private artifact in release: {relative}')
        if path.suffix.lower() == '.gguf' and not (relative.parts[:3] == ('tests', 'fixtures', 'models') and path.stat().st_size <= 1024 * 1024):
            raise ValueError(f'Unapproved model artifact in release: {relative}')
        data = path.read_bytes()
        # Reject accidentally reintroduced local paths or obvious credentials.
        assignment = SECRET_ASSIGNMENT.search(data)
        if assignment and not any(marker in assignment.group(1).lower() for marker in (b'example', b'changeme', b'<', b'${')):
            raise ValueError(f'Credential assignment in release: {path.relative_to(ROOT)}')
        if PRIVATE_PATH.search(data) or SECRET_VALUE.search(data) or any(marker in data for marker in PRIVATE_MARKERS):
            raise ValueError(f'Personal path or credential in release: {path.relative_to(ROOT)}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Pin the gzip header timestamp as well as tar member metadata so repeated
    # exports have the same checksum (useful for release review and CI).
    with destination.open('xb') as raw:
        with gzip.GzipFile(filename='', fileobj=raw, mode='wb', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w') as archive:
                for path in sorted(paths):
                    archive.add(path, arcname=Path('inference-dock') / path.relative_to(ROOT),
                                recursive=False, filter=public_metadata)
    print(f'{hashlib.sha256(destination.read_bytes()).hexdigest()}  {destination}')

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    export(parser.parse_args().destination)
