#!/usr/bin/env python3
"""Static validator for an OpenCode skill directory or Stash plugin directory."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:
    raise SystemExit("PyYAML is required: python -m pip install PyYAML") from exc

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SETTING_TYPES = {"STRING", "NUMBER", "BOOLEAN"}
INTERFACES = {"raw", "rpc", "js"}
CSP_KEYS = {"script-src", "style-src", "connect-src"}

DECLARED_HOOKS_V0311 = {
    f"{obj}.{op}.Post"
    for obj in ("SceneMarker", "Scene", "Image", "Gallery", "GalleryChapter", "Movie", "Group", "Performer", "Studio")
    for op in ("Create", "Update", "Destroy")
} | {"Tag.Create.Post", "Tag.Update.Post", "Tag.Merge.Post", "Tag.Destroy.Post"}


def load_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"YAML parse failed: {exc}") from exc


def local_reference(plugin_dir: Path, manifest: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or value.startswith(("http://", "https://")):
        return None
    value = value.replace("{pluginDir}", str(manifest.parent))
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = manifest.parent / candidate
    try:
        candidate.resolve().relative_to(plugin_dir.resolve())
    except ValueError:
        raise ValueError(f"path escapes plugin directory: {value}")
    return candidate


def validate_skill(directory: Path) -> list[str]:
    errors: list[str] = []
    skill = directory / "SKILL.md"
    if not skill.exists():
        return ["missing SKILL.md"]
    text = skill.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return ["SKILL.md must begin with YAML frontmatter"]
    try:
        _, front, _ = text.split("---", 2)
        metadata = yaml.safe_load(front) or {}
    except Exception as exc:
        return [f"invalid SKILL.md frontmatter: {exc}"]
    allowed = {"name", "description", "license", "compatibility", "metadata"}
    unknown = sorted(set(metadata) - allowed)
    if unknown:
        errors.append(f"unknown OpenCode frontmatter keys: {unknown}")
    name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
        errors.append("frontmatter name must be lowercase alphanumeric with single hyphens")
    elif name != directory.name:
        errors.append(f"frontmatter name {name!r} does not match directory {directory.name!r}")
    if not isinstance(description, str) or not (1 <= len(description) <= 1024):
        errors.append("description must be 1-1024 characters")
    return errors


def validate_plugin(directory: Path) -> list[str]:
    errors: list[str] = []
    warnings: list[str] = []
    manifests = sorted(directory.glob("*.yml")) + sorted(directory.glob("*.yaml"))
    if len(manifests) != 1:
        return [f"expected exactly one manifest in plugin root; found {len(manifests)}"]
    manifest = manifests[0]
    try:
        data = load_yaml(manifest)
    except ValueError as exc:
        return [str(exc)]
    if not isinstance(data, dict):
        return ["manifest root must be a mapping"]

    plugin_id = manifest.stem
    if not SKILL_NAME_RE.fullmatch(plugin_id):
        warnings.append("plugin ID/manifest stem is not lowercase kebab-case")
    if directory.name != plugin_id:
        warnings.append(f"directory {directory.name!r} differs from manifest stem {plugin_id!r}")
    if not data.get("name"):
        errors.append("manifest missing name")

    interface = data.get("interface")
    has_operations = bool(data.get("tasks") or data.get("hooks"))
    if has_operations and interface not in INTERFACES:
        errors.append(f"tasks/hooks require explicit interface in {sorted(INTERFACES)}")

    exec_values = data.get("exec") or []
    if has_operations and not isinstance(exec_values, list):
        errors.append("exec must be a list")
    if interface == "js" and len(exec_values) != 1:
        warnings.append("embedded JS normally has one relative exec path")
    if interface in {"raw", "rpc"} and exec_values:
        for value in exec_values[1:]:
            if isinstance(value, str) and ("/" in value or "\\" in value) and "{pluginDir}" not in value and not Path(value).is_absolute():
                warnings.append(f"external plugin-local path should use {{pluginDir}}: {value}")

    referenced: list[tuple[str, Any]] = []
    if interface == "js" and exec_values:
        referenced.append(("exec", exec_values[0]))
    elif interface in {"raw", "rpc"}:
        for value in exec_values[1:]:
            if isinstance(value, str) and ("{pluginDir}" in value or "/" in value or "\\" in value):
                referenced.append(("exec", value))

    ui = data.get("ui") or {}
    if ui and not isinstance(ui, dict):
        errors.append("ui must be a mapping")
        ui = {}
    for key in ("javascript", "css"):
        values = ui.get(key) or []
        if not isinstance(values, list):
            errors.append(f"ui.{key} must be a list")
        else:
            referenced.extend((f"ui.{key}", value) for value in values)
    csp = ui.get("csp") or {}
    if csp and not isinstance(csp, dict):
        errors.append("ui.csp must be a mapping")
    elif isinstance(csp, dict):
        unknown = sorted(set(csp) - CSP_KEYS)
        if unknown:
            errors.append(f"unsupported v0.31.1 CSP keys: {unknown}")
        for key, values in csp.items():
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                errors.append(f"ui.csp.{key} must be a list of strings")
            elif any(v == "*" for v in values):
                warnings.append(f"ui.csp.{key} contains broad wildcard")

    assets = ui.get("assets") or {}
    if assets and not isinstance(assets, dict):
        errors.append("ui.assets must be a mapping")
    elif isinstance(assets, dict):
        referenced.extend(("ui.assets", value) for value in assets.values())

    settings = data.get("settings") or {}
    if settings and not isinstance(settings, dict):
        errors.append("settings must be a mapping")
    elif isinstance(settings, dict):
        for key, config in settings.items():
            if not isinstance(config, dict):
                errors.append(f"setting {key} must be a mapping")
                continue
            setting_type = str(config.get("type") or "STRING").upper()
            if setting_type not in SETTING_TYPES:
                errors.append(f"setting {key} has invalid type {setting_type}")

    for operation_kind in ("tasks", "hooks"):
        operations = data.get(operation_kind) or []
        if not isinstance(operations, list):
            errors.append(f"{operation_kind} must be a list")
            continue
        names: set[str] = set()
        for index, operation in enumerate(operations):
            where = f"{operation_kind}[{index}]"
            if not isinstance(operation, dict):
                errors.append(f"{where} must be a mapping")
                continue
            name = operation.get("name")
            if not isinstance(name, str) or not name:
                errors.append(f"{where} missing name")
            elif name in names:
                errors.append(f"duplicate operation name: {name}")
            names.add(str(name))
            default_args = operation.get("defaultArgs") or {}
            if not isinstance(default_args, dict):
                errors.append(f"{where}.defaultArgs must be a mapping")
            else:
                non_strings = [k for k, v in default_args.items() if not isinstance(v, str)]
                if non_strings:
                    warnings.append(f"{where}.defaultArgs values should be strings for v0.31.1 compatibility: {non_strings}")
            if operation_kind == "hooks":
                triggers = operation.get("triggeredBy") or []
                if not isinstance(triggers, list) or not triggers:
                    errors.append(f"{where}.triggeredBy must be a non-empty list")
                else:
                    for trigger in triggers:
                        if trigger not in DECLARED_HOOKS_V0311:
                            errors.append(f"unknown v0.31.1 declared hook trigger: {trigger}")
                        elif trigger.startswith("Movie."):
                            warnings.append(f"deprecated trigger, prefer Group and test target build: {trigger}")
                        elif trigger.startswith("Group.") or trigger == "Tag.Merge.Post":
                            warnings.append(f"v0.31.1 source validation discrepancy; integration-test trigger: {trigger}")

    for label, value in referenced:
        try:
            candidate = local_reference(directory, manifest, value)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")
            continue
        if candidate is not None and not candidate.exists():
            errors.append(f"{label} references missing path: {value}")

    # Syntax checks for local source files.
    for py_file in directory.rglob("*.py"):
        proc = subprocess.run([sys.executable, "-m", "py_compile", str(py_file)], capture_output=True, text=True)
        if proc.returncode:
            errors.append(f"Python syntax error in {py_file.name}: {proc.stderr.strip()}")
    node = subprocess.run(["bash", "-lc", "command -v node"], capture_output=True, text=True)
    if node.returncode == 0:
        for js_file in directory.rglob("*.js"):
            proc = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
            if proc.returncode:
                errors.append(f"JavaScript syntax error in {js_file.name}: {proc.stderr.strip()}")

    return errors + [f"WARNING: {warning}" for warning in sorted(set(warnings))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    if not directory.is_dir():
        print(f"ERROR: not a directory: {directory}")
        return 2

    problems = validate_skill(directory) if (directory / "SKILL.md").exists() else validate_plugin(directory)
    errors = [p for p in problems if not p.startswith("WARNING:")]
    for problem in problems:
        prefix = "WARN" if problem.startswith("WARNING:") else "ERROR"
        print(f"{prefix}: {problem.removeprefix('WARNING: ')}")
    if errors:
        print(f"FAILED: {len(errors)} error(s)")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
