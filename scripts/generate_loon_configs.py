#!/usr/bin/env python3
"""Render the public Loon iOS and macOS configurations from one template."""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path


SCALAR_PATTERN = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
MARKER_PATTERN = re.compile(r"^@@([A-Z][A-Z0-9_]*)@@$")


def render(template: str, data: dict[str, object], source: Path) -> str:
    scalar_names = set(SCALAR_PATTERN.findall(template))
    marker_names = {
        match.group(1)
        for line in template.splitlines()
        if (match := MARKER_PATTERN.fullmatch(line))
    }
    expected_names = scalar_names | marker_names
    supplied_names = set(data) - {"output"}
    if expected_names != supplied_names:
        missing = sorted(expected_names - supplied_names)
        unused = sorted(supplied_names - expected_names)
        raise ValueError(f"{source}: template data mismatch; missing={missing}, unused={unused}")

    def replace_scalar(match: re.Match[str]) -> str:
        name = match.group(1)
        value = data[name]
        if not isinstance(value, str):
            raise TypeError(f"{source}: scalar {name} must be a string")
        return value

    rendered_lines: list[str] = []
    for line in template.splitlines():
        marker = MARKER_PATTERN.fullmatch(line)
        if marker:
            name = marker.group(1)
            value = data[name]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise TypeError(f"{source}: marker {name} must be an array of strings")
            rendered_lines.extend(value)
        else:
            rendered_lines.append(SCALAR_PATTERN.sub(replace_scalar, line))

    rendered = "\n".join(rendered_lines) + "\n"
    unresolved = sorted(set(SCALAR_PATTERN.findall(rendered)))
    if unresolved or any(MARKER_PATTERN.fullmatch(line) for line in rendered.splitlines()):
        raise ValueError(f"{source}: unresolved template markers: {unresolved}")
    return rendered


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    template_path = repo_root / "loon" / "templates" / "loon-shared.lcf.tmpl"
    platform_dir = repo_root / "loon" / "platforms"

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="compare generated files without writing")
    args = parser.parse_args()

    template = template_path.read_text(encoding="utf-8")
    definitions = sorted(platform_dir.glob("*.json"))
    if not definitions:
        raise RuntimeError(f"no platform definitions found in {platform_dir}")

    stale = False
    for definition in definitions:
        data = json.loads(definition.read_text(encoding="utf-8"))
        output = repo_root / str(data["output"])
        generated = render(template, data, definition)
        if args.check:
            current = output.read_text(encoding="utf-8") if output.exists() else ""
            if current != generated:
                stale = True
                print(f"OUTDATED: {output}", file=sys.stderr)
                diff = difflib.unified_diff(
                    current.splitlines(),
                    generated.splitlines(),
                    fromfile=str(output),
                    tofile=f"generated from {template_path}",
                    lineterm="",
                )
                print("\n".join(diff), file=sys.stderr)
            else:
                print(f"PASS: {output.name} matches shared template")
        else:
            output.write_text(generated, encoding="utf-8", newline="\n")
            print(f"WROTE: {output}")
    return 1 if stale else 0


if __name__ == "__main__":
    raise SystemExit(main())
