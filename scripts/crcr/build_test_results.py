#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Roll a test cell's shard reports up into a CRCR ``test_results`` summary.

One CRCR HUD result corresponds to one ``(config, arch)`` test cell, but each
cell runs as five parallel shard jobs that each upload their own JUnit tree. This
module unions those shards into the single passed/failed/skipped/total tally the
relay forwards to the HUD, and derives an authoritative conclusion for the cell.

Counting reuses ``scripts/test-summary/parse_failures.py`` so the numbers
reported to the HUD are the same ones the run summary shows -- including its
rerun reconciliation (a test that fails an early attempt and passes a later one
is not counted as a failure) and its de-duplication across shards.

The conclusion deliberately fails closed. It is ``failure`` when any of the
following hold, so missing or truncated evidence can never read as green:

* fewer shard report directories were found than ``--expected-shards`` (any
  shortfall at all, including none found);
* any test is still failing after reconciliation;
* a JUnit header declared more failures than were itemised as cases, which is
  the signature of a process that died before writing them out.

A shard's reports go missing because it timed out, crashed, or was cancelled
before uploading, so a short count is treated as evidence of a broken cell
rather than as a smaller cell. This matters because a shard can go missing
*without* its job going red: ``upload-artifact``'s ``if-no-files-found: warn``
succeeds when a shard produced no reports tree at all, so the shard-job signal
alone would call that cell green.

The caller must additionally AND this against the shard jobs' own GitHub
outcomes -- a shard cancelled after uploading partial XML leaves nothing here
to detect.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# `scripts/test-summary` is not an importable package name (the hyphen is not a
# valid identifier), so add it to the path and import the module directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "test-summary"))

import parse_failures  # noqa: E402


@dataclass
class CellResults:
    """Union of every shard's outcome for one test cell."""

    shards: int = 0
    expected_shards: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    crash_signals: int = 0
    unparsable_reports: int = 0
    failing_tests: list[str] = field(default_factory=list)

    # Only meaningful for a combined row built by `collect_groups`: how many
    # independent cells (e.g. Python versions) went into it, how many were
    # expected, how many shards were short within a group, and how many
    # directories the grouping pattern failed to classify.
    groups: int = 0
    expected_groups: int = 0
    group_shortfalls: int = 0
    unmatched_dirs: int = 0

    @property
    def total(self) -> int:
        return self.passed + self.failed + self.skipped

    @property
    def missing_shards(self) -> int:
        """Shards that were expected but left no reports.

        Zero when the caller did not declare an expectation, which keeps the
        check opt-in rather than silently asserting some default fanout.
        """
        if self.expected_shards <= 0:
            return 0
        return max(0, self.expected_shards - self.shards)

    @property
    def missing_groups(self) -> int:
        """Cells that were expected in a combined row but produced nothing.

        A whole Python version whose shards all died leaves no reports at all,
        which the shard tally alone cannot distinguish from a smaller matrix.
        """
        if self.expected_groups <= 0:
            return 0
        return max(0, self.expected_groups - self.groups)

    @property
    def conclusion(self) -> str:
        # `unparsable_reports` is redundant with `failed` today: the ParseError
        # branch in `parse_failures._collect_xml` also emits a synthetic `crash`
        # failure for the file it could not read, so a truncated report already
        # lands in `failed`. It is named here anyway so this predicate does not
        # depend on that remaining true - a refactor there that stopped
        # synthesising the row would otherwise silently turn corrupt XML green,
        # which is precisely the false green this must never produce.
        if (
            self.shards == 0
            or self.missing_shards > 0
            or self.failed > 0
            or self.crash_signals > 0
            or self.unparsable_reports > 0
            # Combined-row only, all zero for a single cell. A group short of
            # shards can be invisible in the total when another group happens
            # to have run extra, and an unclassified directory means the
            # grouping pattern no longer matches the artifact names, so its
            # results were attributed to nothing.
            or self.missing_groups > 0
            or self.group_shortfalls > 0
            or self.unmatched_dirs > 0
        ):
            return "failure"
        return "success"

    def as_test_results(self) -> dict[str, int]:
        """The payload shape the HUD reads out of ``workflow.test_results``."""
        return {
            "passed": self.passed,
            "failed": self.failed,
            "skipped": self.skipped,
            "total": self.total,
        }


def find_shard_dirs(shards_root: Path) -> dict[str, Path]:
    """Map each immediate subdirectory of ``shards_root`` to its reports tree."""
    if not shards_root.is_dir():
        return {}
    return {p.name: p for p in sorted(shards_root.iterdir()) if p.is_dir()}


def collect_cell(
    shard_dirs: dict[str, Path],
    *,
    parse_logs: bool = True,
    expected_shards: int = 0,
) -> CellResults:
    """Scan every shard and union the outcomes on test identity.

    Each shard is scanned independently so ``parse_failures``' per-shard rerun
    reconciliation applies, then identities are unioned across shards. Counting
    on identity rather than summing per-shard tallies keeps a test that appears
    in several shards' reports from being counted more than once.
    """
    results = CellResults(expected_shards=expected_shards)
    failed_keys: set[tuple[str, str, str]] = set()
    passed_keys: set[tuple[str, str, str]] = set()
    skipped_keys: set[tuple[str, str, str]] = set()
    names: dict[tuple[str, str, str], str] = {}

    for name in sorted(shard_dirs):
        scan = parse_failures.collect(shard_dirs[name], parse_logs=parse_logs)
        results.shards += 1
        results.crash_signals += scan.missing_itemization
        results.unparsable_reports += scan.xml_unparsable
        for failure in scan.failures:
            failed_keys.add(failure.dedup_key)
            names.setdefault(failure.dedup_key, failure.qualified_name)
        passed_keys |= scan.passed_keys
        skipped_keys |= scan.skipped_keys

    results.failed = len(failed_keys)
    results.passed = len(passed_keys - failed_keys)
    results.skipped = len(skipped_keys - failed_keys - passed_keys)
    results.failing_tests = sorted(names[key] for key in failed_keys)
    return results


def group_shard_dirs(
    shard_dirs: dict[str, Path], pattern: re.Pattern[str]
) -> dict[str, dict[str, Path]]:
    """Partition shard directories by ``pattern``'s ``group`` capture.

    Directories whose name does not match are collected under ``""`` so they
    are still counted rather than silently dropped -- an unmatched directory
    means the pattern has drifted from the artifact naming, which the caller
    surfaces as a failure.
    """
    grouped: dict[str, dict[str, Path]] = {}
    for name, path in shard_dirs.items():
        found = pattern.search(name)
        key = found.group("group") if found else ""
        grouped.setdefault(key, {})[name] = path
    return grouped


def collect_groups(
    shard_dirs: dict[str, Path],
    pattern: re.Pattern[str],
    *,
    parse_logs: bool = True,
    expected_shards: int = 0,
    expected_groups: int = 0,
) -> CellResults:
    """Aggregate several independent cells into one combined result.

    Each group is scanned on its own and the *counts* are then summed, rather
    than unioning test identities across the whole set. That distinction is the
    whole point of this function: the groups run the same test suite (WoA
    builds one wheel per Python version and tests each with the same tests), so
    unioning identities would collapse five runs of a test into one and
    under-report the totals roughly fivefold.

    Failing test names are kept per group, tagged with the group, so a test
    that fails on one Python version and passes on another stays attributable.
    """
    grouped = group_shard_dirs(shard_dirs, pattern)
    combined = CellResults(
        expected_shards=expected_shards * max(expected_groups, len(grouped)),
        expected_groups=expected_groups,
        groups=len(grouped),
    )

    for key in sorted(grouped):
        cell = collect_cell(
            grouped[key],
            parse_logs=parse_logs,
            expected_shards=expected_shards,
        )
        combined.shards += cell.shards
        combined.passed += cell.passed
        combined.failed += cell.failed
        combined.skipped += cell.skipped
        combined.crash_signals += cell.crash_signals
        combined.unparsable_reports += cell.unparsable_reports
        combined.group_shortfalls += cell.missing_shards
        label = key or "<unmatched>"
        combined.failing_tests.extend(
            f"{label}: {name}" for name in cell.failing_tests
        )
        if key == "":
            combined.unmatched_dirs = cell.shards

    combined.failing_tests.sort()
    return combined


def render_summary(results: CellResults, title: str, max_rows: int = 25) -> str:
    """Markdown block describing what will be sent to the HUD."""
    lines = [
        f"## {title}",
        "",
        "| field | value |",
        "| --- | --- |",
        f"| shards scanned | `{results.shards}`"
        + (
            f" of `{results.expected_shards}` expected"
            if results.expected_shards
            else ""
        )
        + " |",
        f"| passed | `{results.passed}` |",
        f"| failed | `{results.failed}` |",
        f"| skipped | `{results.skipped}` |",
        f"| total | `{results.total}` |",
        f"| conclusion | `{results.conclusion}` |",
    ]
    if results.groups or results.expected_groups:
        lines.insert(
            4,
            f"| cells combined | `{results.groups}`"
            + (
                f" of `{results.expected_groups}` expected"
                if results.expected_groups
                else ""
            )
            + " |",
        )
    if results.crash_signals:
        lines.append(
            f"| crash signals | `{results.crash_signals}` "
            "(XML headers declared more failures than were itemised) |"
        )
    if results.unparsable_reports:
        lines.append(f"| unparsable reports | `{results.unparsable_reports}` |")
    if results.unmatched_dirs:
        lines.append(
            f"| unclassified shard dirs | `{results.unmatched_dirs}` "
            "(grouping pattern did not match) |"
        )
    if results.shards == 0:
        lines += [
            "",
            "**No shard report directories were found.** Reporting `failure` rather "
            "than assuming success.",
        ]
    elif results.missing_shards:
        lines += [
            "",
            f"**{results.missing_shards} of {results.expected_shards} shards left no "
            "reports.** A shard that uploaded nothing timed out, crashed or was "
            "cancelled, so the counts above are incomplete and the cell is reported "
            "`failure`.",
        ]
    elif results.group_shortfalls:
        lines += [
            "",
            f"**{results.group_shortfalls} shard(s) left no reports.** The totals "
            "across cells can still look complete when one cell is short and "
            "another ran extra, so the shortfall is counted per cell; the result "
            "is reported `failure`.",
        ]
    if results.missing_groups:
        lines += [
            "",
            f"**{results.missing_groups} of {results.expected_groups} cells produced "
            "no reports at all.** An entire cell is missing from the counts above, so "
            "the result is reported `failure`.",
        ]
    if results.failing_tests:
        lines += ["", "### Failing tests", ""]
        lines += [f"- `{name}`" for name in results.failing_tests[:max_rows]]
        if len(results.failing_tests) > max_rows:
            lines.append(
                f"- _... and {len(results.failing_tests) - max_rows} more (truncated)._"
            )
    return "\n".join(lines) + "\n"


def _emit(path: Path | None, text: str) -> None:
    if path is None:
        sys.stdout.write(text)
        return
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--shards-root",
        type=Path,
        required=True,
        help=(
            "Parent directory whose immediate subdirectories are each one shard's "
            "reports tree (i.e. where this cell's test-reports artifacts were "
            "downloaded)."
        ),
    )
    parser.add_argument(
        "--expected-shards",
        type=int,
        default=0,
        help=(
            "How many shards this cell fans out to (per group when "
            "--group-regex is given). Any shortfall in reports is reported as "
            "`failure`. 0 disables the check."
        ),
    )
    parser.add_argument(
        "--group-regex",
        default="",
        metavar="REGEX",
        help=(
            "Regex with a `(?P<group>...)` capture, matched against each shard "
            "directory name, to aggregate several independent cells into one "
            "combined row. Each group is scanned separately and the counts "
            "summed, so cells running the same test suite are not collapsed "
            "onto each other. Omit for a single cell."
        ),
    )
    parser.add_argument(
        "--expected-groups",
        type=int,
        default=0,
        help=(
            "How many groups --group-regex should find. A missing group means "
            "a whole cell left no reports at all, which the shard tally cannot "
            "detect on its own. 0 disables the check."
        ),
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help="Append `key=value` step outputs here (typically $GITHUB_OUTPUT).",
    )
    parser.add_argument(
        "--summary",
        type=Path,
        default=None,
        help="Append the Markdown block here (typically $GITHUB_STEP_SUMMARY).",
    )
    parser.add_argument(
        "--title",
        default="CRCR test results",
        help="Heading for the Markdown block.",
    )
    parser.add_argument(
        "--no-logs",
        action="store_true",
        help="Scan JUnit XML only; skip *.log/*.txt run-log parsing.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Always exits 0: the caller decides what to do with ``conclusion``.

    Exiting non-zero here would fail the reporting job before the callback was
    ever sent, which is precisely the failure mode this pipeline must avoid.
    """
    args = parse_args(argv)

    shard_dirs = find_shard_dirs(args.shards_root)
    if args.group_regex:
        try:
            pattern = re.compile(args.group_regex)
        except re.error as exc:
            print(f"::error title=CRCR test results::bad --group-regex: {exc}")
            pattern = None
        if pattern is None or "group" not in (pattern.groupindex or {}):
            # Cannot group, so cannot report honestly. Fall back to an empty
            # result, which fails closed rather than reporting a wrong total.
            print(
                "::error title=CRCR test results::--group-regex needs a "
                "'(?P<group>...)' capture; reporting failure."
            )
            results = CellResults(expected_shards=max(args.expected_shards, 1))
        else:
            results = collect_groups(
                shard_dirs,
                pattern,
                parse_logs=not args.no_logs,
                expected_shards=args.expected_shards,
                expected_groups=args.expected_groups,
            )
    else:
        results = collect_cell(
            shard_dirs,
            parse_logs=not args.no_logs,
            expected_shards=args.expected_shards,
        )

    outputs = {
        "test-results": json.dumps(results.as_test_results(), separators=(",", ":")),
        "conclusion": results.conclusion,
        "passed": results.passed,
        "failed": results.failed,
        "skipped": results.skipped,
        "total": results.total,
        "shards": results.shards,
        "missing-shards": results.missing_shards,
        "crash-signals": results.crash_signals,
        "groups": results.groups,
        "missing-groups": results.missing_groups,
    }
    _emit(args.github_output, "".join(f"{k}={v}\n" for k, v in outputs.items()))

    if args.summary is not None:
        _emit(args.summary, render_summary(results, args.title))

    print(
        f"cell conclusion={results.conclusion} "
        f"shards={results.shards}/{results.expected_shards or results.shards} "
        f"passed={results.passed} failed={results.failed} skipped={results.skipped}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
