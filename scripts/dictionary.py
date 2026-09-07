#!/usr/bin/env python3
"""Export, extract and compare dictionaries locally; never mutate active rules."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from curator.dictionary_io import (  # noqa: E402
    export_dictionary, read_dictionary, compare_dictionaries, comparison_markdown,
)
from curator.rules import RulesValidationError  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Export the explicitly selected active YAML")
    export.add_argument("rules", type=Path)
    export.add_argument("--output", required=True, type=Path)
    extract = commands.add_parser("extract", help="Recover exact YAML from a checked export")
    extract.add_argument("export", type=Path)
    extract.add_argument("--output", required=True, type=Path)
    compare = commands.add_parser("compare", help="Compare YAML files or dictionary exports")
    compare.add_argument("before", type=Path)
    compare.add_argument("after", type=Path)
    compare.add_argument("--format", choices=["markdown", "json"], default="markdown")
    compare.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            text = json.dumps(export_dictionary(args.rules), ensure_ascii=False, indent=2) + "\n"
        elif args.command == "extract":
            _, text = read_dictionary(args.export)
        else:
            before, _ = read_dictionary(args.before)
            after, _ = read_dictionary(args.after)
            diff = compare_dictionaries(before, after)
            text = json.dumps(diff, ensure_ascii=False, indent=2) + "\n" if args.format == "json" else comparison_markdown(diff)
        if args.output:
            # Exclusive creation avoids accidentally replacing an active file,
            # input export or a previous review, including through a symlink.
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(text.encode("utf-8"))
        else:
            sys.stdout.write(text)
        return 0
    except (OSError, ValueError, TypeError, yaml.YAMLError, RulesValidationError) as exc:
        parser.exit(1, f"Dictionary operation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
