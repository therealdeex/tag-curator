"""Lossless export, corruption rejection, meaningful comparison and task isolation."""

import copy
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from curator.dictionary_io import (
    export_dictionary, read_dictionary, compare_dictionaries, comparison_markdown,
)
from curator.main import _dispatch
from curator.rules import Rules, DEFAULT_RULES_PATH

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def rule_file(tmp_path):
    # Meaningful fixtures include strings the dashboard sanitizer would redact.
    raw = yaml.safe_load(DEFAULT_RULES_PATH.read_text())
    raw["canonical_tags"] = {axis: [] for axis in raw["prefixes"]}
    raw["canonical_tags"]["ACT"] = ["ACT: Kissing"]
    raw["mappings"] = {"custom / source": {
            "disposition": "map", "outputs": ["ACT: Kissing"],
            "provider": ["stashdb", "custom"],
            "notes": "User text: /my/custom/path?api_key=literal — keep exactly",
        }}
    raw["derived"]["country_aliases"] = {"EX": "Example"}
    raw["protected"] = {"prefixes": ["MANUAL:"], "tag_names": ["My custom tag"]}
    path = tmp_path / "rules.yml"
    path.write_bytes(("# Preserve comments and line endings\r\n" + yaml.safe_dump(raw, sort_keys=False).replace("\n", "\r\n")).encode())
    return path


def test_export_roundtrip_is_byte_exact(rule_file, tmp_path):
    before = rule_file.read_bytes()
    payload = export_dictionary(rule_file)
    exported = tmp_path / "export.json"
    exported.write_text(json.dumps(payload))
    raw, text = read_dictionary(exported)
    assert text.encode() == before
    assert rule_file.read_bytes() == before
    assert raw["mappings"]["custom / source"]["provider"] == ["stashdb", "custom"]
    assert payload["rules_sha"] == Rules.from_dict(raw).rules_sha
    assert set(payload) == {"format", "export_version", "exported_at", "schema_version", "rules_sha", "yaml_sha256", "yaml"}


@pytest.mark.parametrize("field,value", [
    ("yaml", "version: 3\n"), ("rules_sha", "wrong"),
    ("schema_version", 2), ("export_version", 99),
])
def test_corrupted_exports_rejected(rule_file, tmp_path, field, value):
    payload = export_dictionary(rule_file)
    payload[field] = value
    exported = tmp_path / "export.json"
    exported.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        read_dictionary(exported)


def test_duplicate_yaml_keys_rejected(rule_file):
    rule_file.write_bytes(rule_file.read_bytes() + b"version: 3\n")
    with pytest.raises(ValueError, match="Duplicate"):
        export_dictionary(rule_file)


def test_valid_yaml_merge_preserved(rule_file):
    raw, _ = read_dictionary(rule_file)
    del raw["mappings"]
    rule_file.write_text(yaml.safe_dump(raw) + """mappings:
  first: &first
    disposition: map
    outputs: ['ACT: Kissing']
  second:
    <<: *first
    notes: inherited output
""")
    payload = export_dictionary(rule_file)
    assert "<<: *first" in payload["yaml"]


def test_compare_ignores_formatting_and_set_order(rule_file, tmp_path):
    raw, _ = read_dictionary(rule_file)
    raw["canonical_tags"]["ACT"].append("ACT: Another")
    raw["mappings"]["custom / source"]["outputs"].append("ACT: Another")
    other = copy.deepcopy(raw)
    other["canonical_tags"]["ACT"].reverse()
    other["mappings"]["custom / source"]["outputs"].reverse()
    other["mappings"][" CUSTOM / SOURCE, "] = other["mappings"].pop("custom / source")
    assert compare_dictionaries(raw, other)["equivalent"]


def test_compare_reports_all_review_sections(rule_file):
    before, _ = read_dictionary(rule_file)
    after = copy.deepcopy(before)
    after["mappings"]["custom / source"] = {"disposition": "ignore"}
    after["mappings"]["new"] = {"disposition": "defer", "notes": "Review"}
    after["derived"]["country_aliases"] = {}
    after["protected"]["tag_names"] = []
    after["canonical_tags"]["ACT"].append("ACT: Another")
    diff = compare_dictionaries(before, after)
    assert not diff["equivalent"]
    assert diff["mappings"]["changed"]["custom / source"]["after"]["disposition"] == "ignore"
    assert "new" in diff["mappings"]["added"]
    assert set(diff["settings"]["changed"]) == {"/protected/tag_names"}
    assert set(diff["settings"]["removed"]) == {"/derived/country_aliases/EX"}
    assert diff["canonical_tags"]["ACT"]["added"] == ["ACT: Another"]
    reverse = compare_dictionaries(after, before)
    assert "new" in reverse["mappings"]["removed"]
    assert reverse["canonical_tags"]["ACT"]["removed"] == ["ACT: Another"]
    assert "provider" in comparison_markdown(diff)
    assert "derived" in comparison_markdown(diff)


def test_comparison_reports_notes_and_precedence_changes(rule_file):
    before, _ = read_dictionary(rule_file)
    after = copy.deepcopy(before)
    after["derived"]["cast_taxonomy"]["gender_order"].reverse()
    after["mappings"]["custom / source"]["notes"] = "changed rationale"
    diff = compare_dictionaries(before, after)
    assert diff["settings"]["changed"]
    assert diff["mappings"]["changed"]


def test_task_is_correlated_lossless_and_does_not_touch_state(rule_file, tmp_path):
    stash = tmp_path / "stash"
    active = stash / "stash-tag-curator-data" / "tag-rules.yml"
    active.parent.mkdir(parents=True)
    active.write_bytes(rule_file.read_bytes())
    envelope = {"args": {"task": "ExportDictionary", "export_request_id": "request-1"},
                "server_connection": {"Dir": str(stash), "PluginDir": str(tmp_path / "plugin")},
                "settings": {"stash_api_key": "must-never-be-exported"}}
    with patch("curator.main.TaskContext.open_state", side_effect=AssertionError("No SQLite access")):
        result = _dispatch(envelope, client=object())
    assert "yaml" not in result
    asset = tmp_path / "plugin/assets/dictionary_export.json"
    payload = json.loads(asset.read_text())
    assert payload["export_request_id"] == "request-1"
    assert payload["yaml"].encode() == rule_file.read_bytes()
    assert "must-never-be-exported" not in asset.read_text()
    assert not (active.parent / "state").exists()
    assert active.read_bytes() == rule_file.read_bytes()
    # A failed next request replaces the previous payload and cannot leak it
    # as a successful new export. Missing active rules must not seed defaults.
    active.unlink()
    envelope["args"]["export_request_id"] = "request-2"
    result = _dispatch(envelope, client=object())
    assert result["error"] == "export_failed"
    payload = json.loads(asset.read_text())
    assert payload["export_request_id"] == "request-2"
    assert "yaml" not in payload
    assert not active.exists()


def test_export_is_one_consistent_read(rule_file):
    original = rule_file.read_bytes()
    read_bytes = Path.read_bytes
    def replace_after_read(path):
        value = read_bytes(path)
        if path == rule_file:
            path.write_text("invalid next version")
        return value
    with patch.object(Path, "read_bytes", replace_after_read):
        result = export_dictionary(rule_file)
    assert result["yaml"].encode() == original


def test_cli_export_extract_compare_and_no_overwrite(rule_file, tmp_path):
    command = [sys.executable, str(ROOT / "scripts/dictionary.py")]
    export = tmp_path / "export.json"
    extract = tmp_path / "extracted.yml"
    for args in [["export", str(rule_file), "--output", str(export)],
                 ["extract", str(export), "--output", str(extract)]]:
        result = subprocess.run(command + args, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    assert extract.read_bytes() == rule_file.read_bytes()
    result = subprocess.run(command + ["compare", str(rule_file), str(export), "--format", "json"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["equivalent"]
    result = subprocess.run(command + ["export", str(rule_file), "--output", str(rule_file)], capture_output=True, text=True)
    assert result.returncode != 0
    assert extract.read_bytes() == rule_file.read_bytes()
