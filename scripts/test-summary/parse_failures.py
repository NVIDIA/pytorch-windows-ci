#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Render failed/errored JUnit test cases as a Markdown summary.

Scans a directory tree for JUnit XML reports, extracts every ``testcase``
that carries a ``<failure>`` or ``<error>`` child, and emits a Markdown
section listing them. Intended for a GitHub Actions per-shard step
summary, so it is read-only and always exits 0 (informational only).
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

FAIL_KINDS = ("error", "failure")


@dataclass(frozen=True)
class Failure:
    classname: str
    name: str
    kind: str
    message: str

    @property
    def qualified_name(self) -> str:
        return f"{self.classname}::{self.name}" if self.classname else self.name


def _first_line(text: str | None, limit: int = 200) -> str:
    """Return a single trimmed line, truncated to ``limit`` characters."""
    if not text:
        return ""
    stripped = text.strip()
    if not stripped:
        return ""
    line = stripped.splitlines()[0]
    return line[:limit] + ("..." if len(line) > limit else "")


def collect_failures(reports_dir: Path) -> tuple[list[Failure], int, int]:
    """Walk ``reports_dir`` for JUnit XML and gather failing test cases.

    Returns ``(failures, files_scanned, files_unparsable)``. Duplicate
    cases (same class + name, e.g. flaky reruns) are collapsed to one
    entry, preferring an ``error`` over a ``failure`` classification.
    """
    seen: dict[tuple[str, str], Failure] = {}
    scanned = 0
    unparsable = 0

    for xml_path in sorted(reports_dir.rglob("*.xml")):
        scanned += 1
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError:
            unparsable += 1
            continue

        for case in root.iter("testcase"):
            for kind in FAIL_KINDS:
                child = case.find(kind)
                if child is None:
                    continue
                failure = Failure(
                    classname=case.get("classname", ""),
                    name=case.get("name", ""),
                    kind=kind,
                    message=_first_line(child.get("message") or child.text),
                )
                key = (failure.classname, failure.name)
                # `error` is listed first in FAIL_KINDS, so an existing
                # error wins; only overwrite when nothing is recorded yet.
                seen.setdefault(key, failure)
                break

    return list(seen.values()), scanned, unparsable


def render_markdown(
    failures: list[Failure],
    scanned: int,
    unparsable: int,
    title: str,
    max_rows: int,
) -> str:
    lines = [f"## {title}", ""]

    if scanned == 0:
        lines.append("No test report XML files were found.")
        return "\n".join(lines) + "\n"

    note = ""
    if unparsable:
        note = f" ({unparsable} unparsable report(s) skipped)"

    if not failures:
        lines.append(f"All collected tests passed across {scanned} report(s).{note}")
        return "\n".join(lines) + "\n"

    lines.append(f"**{len(failures)} failing test(s)** across {scanned} report(s).{note}")
    lines.append("")
    lines.append("| # | Kind | Test | Message |")
    lines.append("| --- | --- | --- | --- |")

    ordered = sorted(failures, key=lambda f: (f.classname, f.name))
    for idx, failure in enumerate(ordered[:max_rows], start=1):
        message = failure.message.replace("|", "\\|") or "-"
        lines.append(
            f"| {idx} | {failure.kind} | `{failure.qualified_name}` | {message} |"
        )

    if len(ordered) > max_rows:
        lines.append("")
        lines.append(f"_... and {len(ordered) - max_rows} more (truncated)._ ")

    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reports-dir",
        type=Path,
        required=True,
        help="Directory tree to scan for JUnit *.xml reports.",
    )
    parser.add_argument(
        "--title",
        default="Failed tests",
        help="Heading for the Markdown section.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="File to append the summary to. Defaults to stdout.",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=200,
        help="Maximum number of failing tests to list before truncating.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    reports_dir = args.reports_dir
    if not reports_dir.is_dir():
        markdown = (
            f"## {args.title}\n\n"
            f"Reports directory `{reports_dir}` does not exist.\n"
        )
        failures: list[Failure] = []
    else:
        failures, scanned, unparsable = collect_failures(reports_dir)
        markdown = render_markdown(
            failures, scanned, unparsable, args.title, args.max_rows
        )

    if args.output is not None:
        with args.output.open("a", encoding="utf-8") as handle:
            handle.write(markdown)
    else:
        sys.stdout.write(markdown)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
