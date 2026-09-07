"""Execute the browser's export-result gate to reject stale and failed jobs."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_export_download_requires_matching_successful_result():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    source = (Path(__file__).resolve().parents[2] / "ui/index.js").read_text()
    start = source.index("  function checkedDictionaryExport(")
    end = source.index("\n  // ------------------------------------------------------------------", start)
    # Run the actual dependency-free browser helper, not a Python replica.
    script = source[start:end] + """
const assert = require('node:assert/strict');
const result = {
  export_request_id: 'new-request', format: 'stash-tag-curator.dictionary',
  export_version: 1, yaml: '# exact text\\n',
  rules_sha: 'a'.repeat(64), yaml_sha256: 'b'.repeat(64)
};
assert.equal(checkedDictionaryExport(result, 'new-request'), result);
assert.throws(() => checkedDictionaryExport(result, 'other-request'), /confirm/);
assert.throws(() => checkedDictionaryExport(null, 'new-request'), /confirm/);
assert.throws(() => checkedDictionaryExport({...result, error: 'export_failed'}, 'new-request'), /failed/);
assert.throws(() => checkedDictionaryExport({...result, yaml: null}, 'new-request'), /incomplete/);
assert.throws(() => checkedDictionaryExport({...result, rules_sha: 'bad'}, 'new-request'), /incomplete/);
assert.throws(() => checkedDictionaryExport({...result, export_version: 2}, 'new-request'), /incomplete/);
"""
    result = subprocess.run([node, "-e", script], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
