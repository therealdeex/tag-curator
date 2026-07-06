#!/usr/bin/env python3
"""Create a Stash plugin from one of the bundled templates."""
from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = SKILL_ROOT / "templates"
VALID_TYPES = sorted(p.name for p in TEMPLATES.iterdir() if p.is_dir())
ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", required=True, choices=VALID_TYPES)
    parser.add_argument("--id", required=True, help="lowercase kebab-case plugin ID")
    parser.add_argument("--name", required=True, help="human-readable plugin name")
    parser.add_argument("--output", type=Path, default=Path.cwd())
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not ID_RE.fullmatch(args.id):
        raise SystemExit("--id must be lowercase kebab-case")

    source = TEMPLATES / args.type
    destination = args.output.expanduser().resolve() / args.id
    if destination.exists():
        if not args.overwrite:
            raise SystemExit(f"destination exists: {destination}")
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    replacements = {
        "__PLUGIN_ID__": args.id,
        "__PLUGIN_NAME__": args.name,
        "__PLUGIN_CSS_ID__": args.id,
    }

    for source_file in source.rglob("*"):
        if not source_file.is_file():
            continue
        relative = source_file.relative_to(source)
        output_name = str(relative).replace("__PLUGIN_ID__", args.id)
        target = destination / output_name
        target.parent.mkdir(parents=True, exist_ok=True)
        text = source_file.read_text(encoding="utf-8")
        for token, value in replacements.items():
            text = text.replace(token, value)
        target.write_text(text, encoding="utf-8")
        if target.suffix == ".py":
            target.chmod(target.stat().st_mode | 0o111)

    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
