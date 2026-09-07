"""Lossless active-rule exports and read-only semantic comparisons.

Exports intentionally preserve rule text (including notes and provider names).
They never include the Stash connection, settings, SQLite state or scene data.
Unlike dashboard snapshots they MUST NOT pass through the report sanitizer.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .rules import Rules

EXPORT_FORMAT = "stash-tag-curator.dictionary"
EXPORT_VERSION = 1


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject silently overwritten keys in review inputs."""


def _mapping(loader, node, deep=False):
    seen = set()
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise ValueError("Duplicate YAML mapping key")
        seen.add(key)
    loader.flatten_mapping(node)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_rules(text: str) -> dict[str, Any]:
    raw = yaml.load(text, Loader=_UniqueKeyLoader)
    Rules.from_dict(raw)
    return raw


def export_dictionary(path: Path) -> dict[str, Any]:
    """Read one file once: no default fallback, initialization or state writes."""
    data = path.read_bytes()
    text = data.decode("utf-8")
    raw = parse_rules(text)
    rules = Rules.from_dict(raw)
    return {
        "format": EXPORT_FORMAT,
        "export_version": EXPORT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": raw["version"],
        "rules_sha": rules.rules_sha,
        "yaml_sha256": hashlib.sha256(data).hexdigest(),
        "yaml": text,
    }


def read_dictionary(path: Path) -> tuple[dict[str, Any], str]:
    """Accept plain rules YAML or an integrity-checked export JSON container."""
    text = path.read_bytes().decode("utf-8")
    document = yaml.load(text, Loader=_UniqueKeyLoader)
    if isinstance(document, dict) and document.get("format") == EXPORT_FORMAT:
        if document.get("export_version") != EXPORT_VERSION:
            raise ValueError("Unsupported dictionary export version")
        text = document.get("yaml")
        if not isinstance(text, str):
            raise ValueError("Export is missing YAML text")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != document.get("yaml_sha256"):
            raise ValueError("Export YAML checksum mismatch")
        raw = parse_rules(text)
        rules = Rules.from_dict(raw)
        if rules.rules_sha != document.get("rules_sha"):
            raise ValueError("Export rules checksum mismatch")
        if raw["version"] != document.get("schema_version"):
            raise ValueError("Export schema version mismatch")
        return raw, text
    Rules.from_dict(document)
    return document, text


def _normalized_mappings(raw):
    result = {}
    for key, rule in raw.get("mappings", {}).items():
        rule = dict(rule)
        if "outputs" in rule:
            rule["outputs"] = sorted(set(rule["outputs"]))
        result[key.strip().lower().rstrip(",")] = rule
    return result


def _changes(before, after):
    return {
        "added": {key: after[key] for key in sorted(after.keys() - before.keys())},
        "removed": {key: before[key] for key in sorted(before.keys() - after.keys())},
        "changed": {
            key: {"before": before[key], "after": after[key]}
            for key in sorted(before.keys() & after.keys()) if before[key] != after[key]
        },
    }


def _setting_changes(before, after, prefix=""):
    result = {"added": {}, "removed": {}, "changed": {}}
    for key in sorted(before.keys() | after.keys()):
        # JSON Pointer escaping makes user-defined keys containing slashes
        # unambiguous; nested settings are shown individually, not as one blob.
        path = prefix + "/" + key.replace("~", "~0").replace("/", "~1")
        if key not in before:
            result["added"][path] = after[key]
        elif key not in after:
            result["removed"][path] = before[key]
        elif isinstance(before[key], dict) and isinstance(after[key], dict):
            nested = _setting_changes(before[key], after[key], path)
            for kind in result:
                result[kind].update(nested[kind])
        elif before[key] != after[key]:
            result["changed"][path] = {"before": before[key], "after": after[key]}
    return result


def compare_dictionaries(before, after):
    """Report data changes without applying them or selecting a winner.

    Mapping output/canonical ordering is immaterial. Derived arrays retain
    their order because ordering can affect matching and precedence.
    """
    mappings = _changes(_normalized_mappings(before), _normalized_mappings(after))
    canonical = {}
    for axis in sorted(before.get("canonical_tags", {}).keys() | after.get("canonical_tags", {}).keys()):
        old = set(before.get("canonical_tags", {}).get(axis, []))
        new = set(after.get("canonical_tags", {}).get(axis, []))
        if old != new:
            canonical[axis] = {"added": sorted(new - old), "removed": sorted(old - new)}
    # Include every other section, including future schema settings. Nothing
    # outside mappings/canonical_tags may silently disappear from the review.
    settings = _setting_changes(
        {k: v for k, v in before.items() if k not in {"mappings", "canonical_tags"}},
        {k: v for k, v in after.items() if k not in {"mappings", "canonical_tags"}},
    )
    return {
        "equivalent": not (any(mappings.values()) or canonical or any(settings.values())),
        "mappings": mappings,
        "canonical_tags": canonical,
        "settings": settings,
    }


def comparison_markdown(diff):
    def cell(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True).replace("&", "&amp;").replace("<", "&lt;").replace("|", "&#124;").replace("`", "&#96;")

    mappings = diff["mappings"]
    lines = ["# Dictionary comparison", "", "No semantic changes." if diff["equivalent"] else
             f"Mappings: {len(mappings['added'])} added, {len(mappings['removed'])} removed, {len(mappings['changed'])} changed.", ""]
    for title, section in [("Mappings", mappings), ("Settings (including derived rules)", diff["settings"])]:
        if not any(section.values()):
            continue
        lines += [f"## {title}", "", "| Change | Key | Before | After |", "|---|---|---|---|"]
        for kind, values in section.items():
            for key, value in values.items():
                old = value.get("before") if kind == "changed" else value if kind == "removed" else None
                new = value.get("after") if kind == "changed" else value if kind == "added" else None
                lines.append(f"| {kind} | {cell(key)} | {cell(old)} | {cell(new)} |")
        lines.append("")
    if diff["canonical_tags"]:
        lines += ["## Canonical categories", "", "| Axis | Added | Removed |", "|---|---|---|"]
        for axis, value in diff["canonical_tags"].items():
            lines.append(f"| {axis} | {cell(value['added'])} | {cell(value['removed'])} |")
        lines.append("")
    return "\n".join(lines)
