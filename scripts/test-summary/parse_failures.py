#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Render failed/errored tests as a Markdown summary.

JUnit XML alone under-reports failures on these runs: a hard crash,
``abort()``, fatal CUDA error, or timeout kills the test process before a
clean ``<testcase>`` is written, so those failures are invisible to a
pure-XML scan. This module combines two evidence sources to get the full
picture:

* **JUnit XML** under the reports tree - ``<testcase>`` ``<failure>`` /
  ``<error>`` cases, plus ``<testsuite>``-level collection/import errors.
  Truncated or unparsable XML (the usual signature of a crash mid-file)
  is surfaced as a ``crash`` row instead of being silently skipped, and
  XML headers that declare more failures than were itemized are flagged.
* **Run logs** (``*.log`` / ``*.txt``) emitted by ``run_test.py`` /
  ``pytest`` - both the short-summary ``FAILED <nodeid> - msg`` form and
  the inline ``<nodeid> FAILED`` progress form, plus per-file
  ``<module> failed!`` markers.

Failures seen in more than one source are de-duplicated (XML wins, since
it carries the richest message). Read-only and always exits 0 - it is an
informational step summary and must never change a job's pass/fail
status.
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

# JUnit ``<testcase>`` / ``<testsuite>`` children that mark a failure.
# ``error`` is first so it wins when a test is recorded as both.
FAIL_KINDS = ("error", "failure")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

# pytest short-summary line: ``FAILED test/foo.py::TestX::test_y - boom``.
# ``search`` (not ``match``) so xdist/rerun prefixes like ``[gw0]`` are
# tolerated; the nodeid must contain ``.py`` to avoid matching the bare
# word ERROR inside tracebacks.
_LOG_SUMMARY = re.compile(
    r"\b(?P<kind>FAILED|ERROR)\s+"
    r"(?P<nodeid>\S+\.py(?:::\S+)?)"
    r"(?:\s+-\s+(?P<msg>.*))?\s*$"
)
# pytest inline-progress line: ``test/foo.py::TestX::test_y FAILED [ 50%]``.
_LOG_INLINE = re.compile(
    r"(?P<nodeid>\S+\.py(?:::\S+)?)\s+(?P<kind>FAILED|ERROR)\b"
)
# pytest inline-progress *pass* line: ``test/foo.py::TestX::test_y PASSED``.
# Only used to reconcile flaky reruns (fail on one attempt, pass on a later
# one); it never adds a failure.
_LOG_PASSED = re.compile(r"(?P<nodeid>\S+\.py(?:::\S+)?)\s+PASSED\b")
# run_test.py per-file marker: ``test_foo failed!`` (the ``test`` guard in
# the caller keeps this from matching generic ``... failed`` prose).
_LOG_RUNTEST = re.compile(r"^(?P<file>[\w./\\-]+)\s+failed!?\s*$")


@dataclass(frozen=True)
class Failure:
    classname: str
    name: str
    kind: str
    message: str
    file: str = ""
    # Provenance of the record: "xml", "log", or "report" (a crashed /
    # unparsable JUnit file surfaced as a single crash row).
    source: str = "xml"

    @property
    def module(self) -> str:
        """Best-effort test module (e.g. ``test_torchbind``).

        Prefers the JUnit ``file`` attribute; falls back to the dotted
        prefix of ``classname`` (``a.b.TestX`` -> ``a.b``).
        """
        if self.file:
            return Path(self.file.replace("\\", "/")).stem
        if "." in self.classname:
            return self.classname.rsplit(".", 1)[0]
        return ""

    @property
    def _class_leaf(self) -> str:
        """Trailing class segment, dropping any module/path prefix."""
        if not self.classname:
            return ""
        return self.classname.replace("::", ".").split(".")[-1]

    @property
    def dedup_key(self) -> tuple[str, str, str]:
        """Source-agnostic identity used to merge XML and log records.

        Normalises so a pytest nodeid (``test_x.py::TestA::test_y``) and a
        JUnit case (file ``test_x.py``, classname ``test_x.TestA``, name
        ``test_y``) collapse to the same key. A class segment equal to the
        module (the function-style ``classname == module`` case) is treated
        as "no class" so both spellings line up.
        """
        if self.source == "report":
            return ("__report__", self.file, "")
        module = self.module.lower()
        leaf = self._class_leaf.lower()
        if leaf == module:
            leaf = ""
        return (module, leaf, self.name.lower())

    @property
    def qualified_name(self) -> str:
        if self.source == "report":
            return self.name
        base = f"{self.classname}::{self.name}" if self.classname else self.name
        module = self.module
        # Only prepend the module when it is not already part of classname
        # (pytest-style ``test_mod.TestX`` already embeds it).
        if module and not self.classname.startswith(module):
            return f"{module}::{base}"
        return base


@dataclass
class ScanResult:
    """Aggregated outcome of scanning a reports tree."""

    failures: list[Failure] = field(default_factory=list)
    xml_scanned: int = 0
    xml_unparsable: int = 0
    xml_itemized: int = 0
    reported_failures: int = 0
    logs_scanned: int = 0

    @property
    def scanned_anything(self) -> bool:
        return self.xml_scanned > 0 or self.logs_scanned > 0

    @property
    def missing_itemization(self) -> int:
        """XML-header failures that were never itemized as cases.

        A positive value means the harness counted more failures/errors in
        a ``<testsuite>`` header than it wrote out as individual cases -
        another crash signature.
        """
        return max(0, self.reported_failures - self.xml_itemized)


def _first_line(text: str | None, limit: int = 200) -> str:
    """Return a single trimmed line, truncated to ``limit`` characters."""
    if not text:
        return ""
    stripped = text.strip()
    if not stripped:
        return ""
    line = stripped.splitlines()[0]
    return line[:limit] + ("..." if len(line) > limit else "")


def _normalize_kind(token: str) -> str:
    """Map a log token (``FAILED`` / ``ERROR``) to a JUnit-style kind."""
    return "error" if token.upper() == "ERROR" else "failure"


def _parse_nodeid(nodeid: str) -> tuple[str, str, str]:
    """Split a pytest nodeid into ``(file, classname, name)``.

    Handles ``file.py``, ``file.py::test_fn``, and
    ``file.py::TestClass::test_fn`` (the extra ``::`` segments, if any,
    are folded back into the name).
    """
    parts = nodeid.split("::")
    file = parts[0]
    if len(parts) >= 3:
        return file, parts[1], "::".join(parts[2:])
    if len(parts) == 2:
        return file, "", parts[1]
    return file, "", ""


def _failure_from_log_line(line: str) -> Failure | None:
    """Extract a single failure from one (ANSI-stripped) log line, if any."""
    summary = _LOG_SUMMARY.search(line)
    if summary:
        return _failure_from_nodeid(
            summary.group("nodeid"),
            summary.group("kind"),
            summary.group("msg"),
        )

    inline = _LOG_INLINE.search(line)
    if inline:
        return _failure_from_nodeid(inline.group("nodeid"), inline.group("kind"), None)

    runtest = _LOG_RUNTEST.match(line.strip())
    if runtest:
        file = runtest.group("file")
        if "test" in file.lower():
            return Failure(
                classname="",
                name=Path(file.replace("\\", "/")).stem,
                kind="error",
                message="reported as failed by run_test.py",
                file=file,
                source="log",
            )
    return None


def _failure_from_nodeid(nodeid: str, kind: str, msg: str | None) -> Failure:
    file, classname, name = _parse_nodeid(nodeid)
    if not name:
        name = Path(file.replace("\\", "/")).stem
    return Failure(
        classname=classname,
        name=name,
        kind=_normalize_kind(kind),
        message=_first_line(msg),
        file=file,
        source="log",
    )


def _passed_key_from_log_line(line: str) -> tuple[str, str, str] | None:
    """Dedup key for a pytest ``... PASSED`` progress line, else ``None``.

    The key is computed the same way as a failure's :attr:`Failure.dedup_key`
    so a passing rerun line reconciles against the matching failing record
    regardless of whether that failure came from XML or a log.
    """
    match = _LOG_PASSED.search(line)
    if match is None:
        return None
    file, classname, name = _parse_nodeid(match.group("nodeid"))
    if not name:
        name = Path(file.replace("\\", "/")).stem
    return Failure(
        classname=classname,
        name=name,
        kind="passed",
        message="",
        file=file,
        source="log",
    ).dedup_key


def _failure_from_testcase(case: ET.Element) -> Failure | None:
    """Build a ``Failure`` from a ``<testcase>`` that failed or errored.

    Returns ``None`` for a pass, a ``<skipped>`` case, or a bare rerun
    record, so the caller can tell failing attempts apart from clean ones.
    ``error`` is preferred over ``failure`` when both are present.
    """
    for kind in FAIL_KINDS:
        child = case.find(kind)
        if child is None:
            continue
        return Failure(
            classname=case.get("classname", ""),
            name=case.get("name", ""),
            kind=kind,
            message=_first_line(child.get("message") or child.text),
            file=case.get("file", ""),
            source="xml",
        )
    return None


def _case_passed(case: ET.Element) -> bool:
    """True when a ``<testcase>`` is a clean pass (no failure/error/skip)."""
    return all(case.find(kind) is None for kind in (*FAIL_KINDS, "skipped"))


def _testcase_key(case: ET.Element) -> tuple[str, str, str]:
    """Dedup key for a ``<testcase>`` (see :attr:`Failure.dedup_key`)."""
    return Failure(
        classname=case.get("classname", ""),
        name=case.get("name", ""),
        kind="passed",
        message="",
        file=case.get("file", ""),
        source="xml",
    ).dedup_key


def _collect_xml(
    reports_dir: Path,
    result: ScanResult,
    sink: _FailureSink,
    passed: set[tuple[str, str, str]],
) -> None:
    """Scan JUnit XML, recording cases, suite-level errors, and crashes.

    Passing ``<testcase>`` keys are added to ``passed`` so flaky reruns
    (fail on one attempt, pass on a later one) can be reconciled later.
    """
    for xml_path in sorted(reports_dir.rglob("*.xml")):
        result.xml_scanned += 1
        try:
            root = ET.parse(xml_path).getroot()
        except ET.ParseError:
            result.xml_unparsable += 1
            sink.add(
                Failure(
                    classname="",
                    name=xml_path.name,
                    kind="crash",
                    message="JUnit report unparsable - process likely crashed (truncated XML).",
                    file=str(xml_path),
                    source="report",
                )
            )
            continue

        for suite in root.iter("testsuite"):
            for attr in ("failures", "errors"):
                try:
                    result.reported_failures += int(suite.get(attr, "0") or "0")
                except ValueError:
                    pass

        for case in root.iter("testcase"):
            failure = _failure_from_testcase(case)
            if failure is not None:
                result.xml_itemized += 1
                sink.add(failure, prefer_error=True)
            elif _case_passed(case):
                passed.add(_testcase_key(case))

        # Collection / import errors are emitted as direct children of
        # ``<testsuite>`` (not wrapped in a ``<testcase>``).
        for suite in root.iter("testsuite"):
            for kind in FAIL_KINDS:
                child = suite.find(kind)
                if child is None:
                    continue
                result.xml_itemized += 1
                sink.add(
                    Failure(
                        classname="",
                        name=suite.get("name", "") or xml_path.stem,
                        kind=kind,
                        message=_first_line(child.get("message") or child.text),
                        file=suite.get("file", ""),
                        source="xml",
                    )
                )
                break


def _collect_logs(
    reports_dir: Path,
    result: ScanResult,
    sink: _FailureSink,
    passed: set[tuple[str, str, str]],
) -> None:
    """Scan ``*.log`` / ``*.txt`` for failures not captured in XML.

    ``PASSED`` progress lines are recorded in ``passed`` so a rerun that
    eventually passed cancels an earlier failing line for the same test.
    """
    log_paths = sorted(
        {*reports_dir.rglob("*.log"), *reports_dir.rglob("*.txt")}
    )
    for log_path in log_paths:
        result.logs_scanned += 1
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            clean = _ANSI.sub("", raw).rstrip()
            failure = _failure_from_log_line(clean)
            if failure is not None:
                sink.add(failure)
                continue
            passed_key = _passed_key_from_log_line(clean)
            if passed_key is not None:
                passed.add(passed_key)


class _FailureSink:
    """De-duplicating collector keyed on :attr:`Failure.dedup_key`.

    First record for a key wins, except an ``error`` may upgrade a stored
    ``failure`` (``prefer_error``). XML is added before logs, so a log
    record never displaces the richer XML message for the same test.
    """

    def __init__(self) -> None:
        self._seen: dict[tuple[str, str, str], Failure] = {}

    def add(self, failure: Failure, *, prefer_error: bool = False) -> None:
        key = failure.dedup_key
        existing = self._seen.get(key)
        if existing is None:
            self._seen[key] = failure
        elif (
            prefer_error
            and failure.kind == "error"
            and existing.kind == "failure"
            and existing.source == failure.source
        ):
            self._seen[key] = failure

    def values(self) -> list[Failure]:
        return list(self._seen.values())


def collect(reports_dir: Path, *, parse_logs: bool = True) -> ScanResult:
    """Scan ``reports_dir`` for failures across JUnit XML and run logs.

    Flaky reruns are reconciled away: PyTorch's ``run_test.py`` retries a
    failing test (pytest ``--reruns`` with ``--junit-xml-reruns`` records
    each attempt in one report, and whole-process stepcurrent retries write a
    fresh report while leaving the failing one behind), so a test that fails
    an early attempt but passes a later one appears as *both* a failing and a
    passing record for the same ``(module, class, name)``. CI scores such a
    test as a pass, so any failure whose test was also seen passing - in any
    report or log - is dropped.
    """
    result = ScanResult()
    sink = _FailureSink()
    passed: set[tuple[str, str, str]] = set()
    _collect_xml(reports_dir, result, sink, passed)
    if parse_logs:
        _collect_logs(reports_dir, result, sink, passed)
    result.failures = [f for f in sink.values() if f.dedup_key not in passed]
    return result


def render_markdown(result: ScanResult, title: str, max_rows: int) -> str:
    """Render a :class:`ScanResult` as a Markdown section."""
    lines = [f"## {title}", ""]

    if not result.scanned_anything:
        lines.append("No test report XML files or run logs were found.")
        return "\n".join(lines) + "\n"

    notes: list[str] = []
    if result.xml_unparsable:
        notes.append(
            f"{result.xml_unparsable} unparsable/truncated report(s) surfaced as crash row(s)."
        )
    if result.missing_itemization:
        notes.append(
            f"{result.missing_itemization} failure(s) declared in XML headers "
            "but not itemized (likely crash)."
        )
    note = f" ({'; '.join(notes)})" if notes else ""

    scanned_desc = f"{result.xml_scanned} report(s) and {result.logs_scanned} log(s)"

    if not result.failures:
        lines.append(f"All collected tests passed across {scanned_desc}.{note}")
        return "\n".join(lines) + "\n"

    by_source = Counter(f.source for f in result.failures)
    source_desc = ", ".join(
        f"{count} {label}"
        for label, count in (
            ("from XML", by_source.get("xml", 0)),
            ("from logs", by_source.get("log", 0)),
            ("crashed report(s)", by_source.get("report", 0)),
        )
        if count
    )
    lines.append(
        f"**{len(result.failures)} failing/errored item(s)** "
        f"({source_desc}) across {scanned_desc}.{note}"
    )
    lines.append("")
    lines.append("| # | Kind | Source | Test | Message |")
    lines.append("| --- | --- | --- | --- | --- |")

    ordered = sorted(result.failures, key=lambda f: (f.module, f.classname, f.name))
    for idx, failure in enumerate(ordered[:max_rows], start=1):
        message = failure.message.replace("|", "\\|") or "-"
        lines.append(
            f"| {idx} | {failure.kind} | {failure.source} "
            f"| `{failure.qualified_name}` | {message} |"
        )

    if len(ordered) > max_rows:
        lines.append("")
        lines.append(f"_... and {len(ordered) - max_rows} more (truncated)._ ")

    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the failure-summary CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reports-dir",
        type=Path,
        required=True,
        help="Directory tree to scan for JUnit *.xml reports and *.log/*.txt run logs.",
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
    parser.add_argument(
        "--no-logs",
        action="store_true",
        help="Scan JUnit XML only; skip *.log/*.txt run-log parsing.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Collect failures and write the Markdown summary to the output sink.

    Always returns 0: this is an informational step that must not change a
    job's pass/fail status.
    """
    args = parse_args(argv)

    reports_dir = args.reports_dir
    if not reports_dir.is_dir():
        markdown = (
            f"## {args.title}\n\n"
            f"Reports directory `{reports_dir}` does not exist.\n"
        )
    else:
        result = collect(reports_dir, parse_logs=not args.no_logs)
        markdown = render_markdown(result, args.title, args.max_rows)

    if args.output is not None:
        with args.output.open("a", encoding="utf-8") as handle:
            handle.write(markdown)
    else:
        sys.stdout.write(markdown)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
