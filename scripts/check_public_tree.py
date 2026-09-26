"""Bounded public-tree lint, not a guarantee against every possible secret."""
import ast
import hashlib
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
ALLOW = {
    '.gitattributes', '.gitignore', '.github/workflows/verify.yml',
    'LICENSE', 'README.md', 'frame-stream-audio-fix.sh',
    'docs/DESIGN.md', 'docs/INSTALL-REVIEW.md', 'docs/PUBLICATION.md',
    'scripts/check_public_tree.py', 'hardened/LICENSE', 'hardened/SHA256SUMS',
    'hardened/frame_audio_guard.py', 'hardened/frame-stream-audio-fix',
    'hardened/frame-stream-audio-fix.service', 'hardened/test_guard.py',
    'hardened/reproduce_upstream_filter.sh',
}
files = set(subprocess.check_output(['git', 'ls-files'], cwd=ROOT, text=True).splitlines())
assert files == ALLOW, 'Unexpected/missing tracked files: ' + str(sorted(files ^ ALLOW))
patterns = [
    r'-----BEGIN (?:OPENSSH|RSA|EC|DSA|ENCRYPTED) PRIVATE KEY-----',
    r'\bgh[pousr]_[A-Za-z0-9]{20,}', r'\bgithub_pat_[A-Za-z0-9_]{20,}',
    r'\bAKIA[A-Z0-9]{16}\b', r'\bsk-[A-Za-z0-9]{24,}',
    r'\b[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/]',
    r'\bDESKTOP-[A-Z0-9]+\b', r'\b192\.168\.[0-9]+\.[0-9]+\b',
    r'(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b',
]
for name in sorted(files):
    data = (ROOT / name).read_bytes()
    text = data.decode('utf-8')
    assert len(data) < 100_000, 'Unexpected oversized file: ' + name
    for pattern in patterns:
        assert not re.search(pattern, text), 'Potential private material in: ' + name
    if name.endswith('.py'):
        ast.parse(text, filename=name)
for line in (ROOT / 'hardened/SHA256SUMS').read_text().splitlines():
    digest, name = line.split('  ', 1)
    assert name in {'LICENSE', 'frame-stream-audio-fix', 'frame_audio_guard.py', 'frame-stream-audio-fix.service'}
    assert hashlib.sha256((ROOT / 'hardened' / name).read_bytes()).hexdigest() == digest, name
assert (ROOT / 'LICENSE').read_bytes() == (ROOT / 'hardened/LICENSE').read_bytes()
assert hashlib.sha256((ROOT / 'frame-stream-audio-fix.sh').read_bytes()).hexdigest() == 'ee3963c30579ac9ab9002a6503e776b72e2de8a321d9f4224c5f8bcba4f83eb9'
print('PUBLIC_TREE_CHECK_PASS: allowlist, bounded pattern scan, AST, runtime hashes, original script/license')
