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
from collections import Counter, defaultdict
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
# pytest *pass* progress lines, used only to reconcile flaky reruns (fail on
# one attempt, pass on a later one); they never add a failure. Two orderings
# must both be recognised, mirroring the failure patterns above, otherwise a
# pass in the form the failure was NOT seen in slips through and the failure
# is never cancelled:
#   nodeid-first (serial):  ``test/foo.py::TestX::test_y PASSED [ 50%]``
#   result-first (xdist):   ``[gw0] [ 50%] PASSED test/foo.py::TestX::test_y``
# ``\bPASSED`` avoids matching ``XPASSED`` (no word boundary before ``P``).
_LOG_PASSED = re.compile(r"(?P<nodeid>\S+\.py(?:::\S+)?)\s+PASSED\b")
_LOG_PASSED_PREFIX = re.compile(r"\bPASSED\s+(?P<nodeid>\S+\.py(?:::\S+)?)")
# run_test.py per-file marker. Two observed forms:
#   ``test_foo failed!``                                  (bare)
#   ``cpp_extensions/test_libtorch_agnostic 1/1 failed!``  (with the
#     ``<index>/<total>`` progress token run_test.py prints per file).
# The optional ``\d+/\d+`` group absorbs that token; the ``test`` guard in
# the caller keeps this from matching generic ``... failed`` prose.
_LOG_RUNTEST = re.compile(r"^(?P<file>[\w./\\-]+)(?:\s+\d+/\d+)?\s+failed!?\s*$")


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
    def module_path(self) -> str:
        """Path-aware module identity used for de-duplication.

        A normalised, test-directory-relative path with the ``.py`` suffix
        dropped (``functorch/test_ops.py`` -> ``functorch/test_ops``).
        Keeping the directory is what stops same-basename tests in different
        directories (``test_ops.py`` and ``functorch/test_ops.py``) from
        collapsing to one key and reconciling each other's failures. The
        leading ``test/`` (PyTorch's test root) is stripped so a repo-relative
        spelling (``test/test_nn.py`` in a log) and a test-root-relative one
        (``test_nn.py`` in JUnit) line up. Falls back to the dotted classname
        prefix when no ``file`` is known.
        """
        if self.file:
            path = self.file.replace("\\", "/")
            while path.startswith("./"):
                path = path[2:]
            if path.startswith("test/"):
                path = path[len("test/"):]
            if path.endswith(".py"):
                path = path[:-3]
            return path
        if "." in self.classname:
            return self.classname.rsplit(".", 1)[0].replace(".", "/")
        return ""

    @property
    def module(self) -> str:
        """Best-effort test module basename (e.g. ``test_torchbind``).

        The trailing path segment of :attr:`module_path`, used for display
        and sorting only - never for identity (that is :attr:`dedup_key`).
        """
        path = self.module_path
        return path.rsplit("/", 1)[-1] if path else ""

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
        ``test_y``) collapse to the same key. The path-aware
        :attr:`module_path` is the module component, so tests that share a
        basename but live in different directories stay distinct. A class
        segment equal to the module basename (the function-style
        ``classname == module`` case) is treated as "no class" so both
        spellings line up.
        """
        if self.source == "report":
            return ("__report__", self.file, "")
        module_path = self.module_path.lower()
        module_base = module_path.rsplit("/", 1)[-1]
        leaf = self._class_leaf.lower()
        if leaf == module_base:
            leaf = ""
        return (module_path, leaf, self.name.lower())

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
    # Unique ``Failure.dedup_key`` identities seen passing / skipped in JUnit
    # XML ``<testcase>`` records. Used for the collected/passed/skipped totals.
    passed_keys: set[tuple[str, str, str]] = field(default_factory=set)
    skipped_keys: set[tuple[str, str, str]] = field(default_factory=set)
    # Latest ``<testsuite timestamp=...>`` at which each key was last seen
    # failing vs. last seen recovered (a clean pass or a skip). These drive
    # attempt-ordered rerun reconciliation: a failure is only cancelled when
    # the recovering evidence is at least as recent as the failing evidence,
    # so an earlier pass can never mask a genuinely later failure. Timestamps
    # are compared as ISO-8601 strings; a missing timestamp sorts earliest
    # ("") and falls back to lenient "seen recovered anywhere" behaviour.
    xml_fail_ts: dict[tuple[str, str, str], str] = field(default_factory=dict)
    xml_ok_ts: dict[tuple[str, str, str], str] = field(default_factory=dict)
    # Keys seen passing in a ``PASSED`` run-log progress line. Logs carry no
    # per-attempt timestamp, but a ``PASSED`` line is only ever emitted by an
    # attempt that actually passed, so it always counts as recovery.
    passed_log_keys: set[tuple[str, str, str]] = field(default_factory=set)

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

    @property
    def _failed_keys(self) -> set[tuple[str, str, str]]:
        return {f.dedup_key for f in self.failures}

    @property
    def failed_count(self) -> int:
        """Reconciled failing/errored items (matches the rendered table)."""
        return len(self.failures)

    @property
    def passed_count(self) -> int:
        """Unique tests that passed and are not counted as a failure.

        Best-effort and XML-derived: run logs do not reliably enumerate every
        passing test, so passes/skips are tallied from JUnit ``<testcase>``
        records only.
        """
        return len(self.passed_keys - self._failed_keys)

    @property
    def skipped_count(self) -> int:
        """Unique skipped tests that neither passed nor failed elsewhere."""
        return len(self.skipped_keys - self._failed_keys - self.passed_keys)

    @property
    def total_count(self) -> int:
        """Distinct tests accounted for: passed + failed + skipped."""
        return self.passed_count + self.failed_count + self.skipped_count


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
    regardless of whether that failure came from XML or a log. Both the
    serial (nodeid-first) and xdist (result-first) progress orderings are
    accepted.
    """
    match = _LOG_PASSED.search(line) or _LOG_PASSED_PREFIX.search(line)
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


def _bump(store: dict[tuple[str, str, str], str], key: tuple[str, str, str], ts: str) -> None:
    """Record ``ts`` for ``key`` when it is newer than what is stored."""
    current = store.get(key)
    if current is None or ts > current:
        store[key] = ts


def _collect_xml(reports_dir: Path, result: ScanResult, sink: _FailureSink) -> None:
    """Scan JUnit XML, recording cases, suite-level errors, and crashes.

    Each ``<testcase>`` outcome is tagged with its ``<testsuite>``
    ``timestamp`` so a later-attempt pass/skip can be told apart from an
    earlier one during rerun reconciliation.
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
            ts = suite.get("timestamp", "") or ""
            for attr in ("failures", "errors"):
                try:
                    result.reported_failures += int(suite.get(attr, "0") or "0")
                except ValueError:
                    pass

            # ``findall`` (direct children only) so a testcase under a nested
            # suite is attributed to that suite's timestamp exactly once.
            for case in suite.findall("testcase"):
                failure = _failure_from_testcase(case)
                if failure is not None:
                    result.xml_itemized += 1
                    sink.add(failure, prefer_error=True)
                    _bump(result.xml_fail_ts, failure.dedup_key, ts)
                elif _case_passed(case):
                    key = _testcase_key(case)
                    result.passed_keys.add(key)
                    _bump(result.xml_ok_ts, key, ts)
                else:
                    # Neither failed nor a clean pass -> a ``<skipped>`` case.
                    key = _testcase_key(case)
                    result.skipped_keys.add(key)
                    _bump(result.xml_ok_ts, key, ts)

            # Collection / import errors are emitted as direct children of
            # ``<testsuite>`` (not wrapped in a ``<testcase>``).
            for kind in FAIL_KINDS:
                child = suite.find(kind)
                if child is None:
                    continue
                result.xml_itemized += 1
                suite_failure = Failure(
                    classname="",
                    name=suite.get("name", "") or xml_path.stem,
                    kind=kind,
                    message=_first_line(child.get("message") or child.text),
                    file=suite.get("file", ""),
                    source="xml",
                )
                sink.add(suite_failure)
                _bump(result.xml_fail_ts, suite_failure.dedup_key, ts)
                break


def _collect_logs(reports_dir: Path, result: ScanResult, sink: _FailureSink) -> None:
    """Scan ``*.log`` / ``*.txt`` for failures not captured in XML.

    ``PASSED`` progress lines are recorded so a rerun that eventually passed
    cancels an earlier failing line for the same test.
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
                result.passed_log_keys.add(passed_key)


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


def _is_recovered(key: tuple[str, str, str], result: ScanResult) -> bool:
    """Whether a failing ``key`` was superseded by a later successful attempt.

    Recovery is tied to the *latest* evidence, not to a pass being seen
    anywhere, so an early pass can never mask a genuinely later failure:

    * A ``PASSED`` run-log progress line always counts - such a line is only
      emitted by an attempt that actually passed, and logs carry no
      comparable timestamp.
    * Otherwise a clean/skipped XML case recovers the key only when its
      ``<testsuite>`` timestamp is at least as recent as the newest failing
      XML timestamp for the same key. Missing timestamps compare equal (""),
      preserving lenient "recovered anywhere" behaviour when a harness omits
      them.
    """
    if key in result.passed_log_keys:
        return True
    ok_ts = result.xml_ok_ts.get(key)
    if ok_ts is None:
        return False
    fail_ts = result.xml_fail_ts.get(key)
    return fail_ts is None or ok_ts >= fail_ts


def collect(reports_dir: Path, *, parse_logs: bool = True) -> ScanResult:
    """Scan ``reports_dir`` for failures across JUnit XML and run logs.

    Flaky reruns are reconciled away: PyTorch's ``run_test.py`` retries a
    failing test (pytest ``--reruns`` with ``--junit-xml-reruns`` records
    each attempt in one report, and whole-process stepcurrent retries write a
    fresh report while leaving the failing one behind), so a test that fails
    an early attempt but recovers on a later one appears as *both* a failing
    and a recovered record for the same ``(module_path, class, name)``. A
    later attempt counts as recovered when it either **passes** (clean XML
    case or a ``PASSED`` log line) or is **skipped** in XML - e.g. a profiler
    test that errors with "External init callback ..." on a crashed attempt
    and is then ``@skipCUDAIf`` skipped on the clean rerun.

    Recovery is scoped tightly (see :func:`_is_recovered`): it is keyed on the
    path-aware :attr:`Failure.module_path` (so a same-basename test in another
    directory cannot cancel a failure), and, within XML, honours attempt
    order via ``<testsuite>`` timestamps so an earlier pass never hides a
    later failure.

    A whole-file ``<file> failed!`` marker from ``run_test.py`` does not need
    attempt reconciliation: that line is only printed by ``handle_complete``
    after ``run_test_retries`` has exhausted its retries on a test that
    ``FAILED CONSISTENTLY`` (>=3 attempts) - a fail-then-pass instead hits the
    "Test succeeded in new process, continuing" branch and prints no marker,
    so a stale marker cannot survive a successful retry.
    """
    result = ScanResult()
    sink = _FailureSink()
    _collect_xml(reports_dir, result, sink)
    if parse_logs:
        _collect_logs(reports_dir, result, sink)
    result.failures = [
        f for f in sink.values() if not _is_recovered(f.dedup_key, result)
    ]
    return result


def _totals_line(result: ScanResult) -> str:
    """One-line passed/failed/skipped tally over the distinct tests seen."""
    return (
        f"**{result.total_count} test(s) collected:** "
        f"{result.passed_count} passed, "
        f"{result.failed_count} failed/errored, "
        f"{result.skipped_count} skipped."
    )


def render_markdown(result: ScanResult, title: str, max_rows: int) -> str:
    """Render a :class:`ScanResult` as a Markdown section."""
    lines = [f"## {title}", ""]

    if not result.scanned_anything:
        lines.append("No test report XML files or run logs were found.")
        return "\n".join(lines) + "\n"

    lines.append(_totals_line(result))
    lines.append("")

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
        if result.missing_itemization:
            # A header that declares more failures/errors than were written as
            # cases is a crash/timeout signature (the process died before the
            # cases reached the report). Do not call this shard green.
            lines.append(
                f"**Likely crash:** no failing tests were itemized, but XML "
                f"headers across {scanned_desc} declared "
                f"{result.missing_itemization} more failure(s)/error(s) than "
                f"were recorded as cases - the process probably died before "
                f"writing them. Treat this shard as failed and inspect the raw "
                f"logs.{note}"
            )
        else:
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


# --------------------------------------------------------------------------
# Cross-shard aggregation
# --------------------------------------------------------------------------
_SHARD_TOKEN = re.compile(r"shard[-_]?(\d+)", re.IGNORECASE)
# ``test-reports-<cell>-shard<N>-<run>-<attempt>``: capture the cell (the
# build-env / python / cuda / arch tuple) and the shard number. The cell is
# what groups shards into separate per-cell aggregates.
_CELL_SHARD = re.compile(
    r"^(?:test-reports-)?(?P<cell>.*?)-?shard[-_]?(?P<shard>\d+)", re.IGNORECASE
)


def _shard_label(name: str) -> str:
    """Short, human-friendly shard label derived from an artifact/dir name.

    PyTorch test-report artifacts are named
    ``test-reports-<cell>-shard<N>-<run>-<attempt>``; extract just ``<N>`` for
    a compact "Shards" column. Falls back to the raw name when no shard token
    is present.
    """
    match = _SHARD_TOKEN.search(name)
    return match.group(1) if match else name


def _cell_label(name: str) -> str:
    """Cell (matrix cell) an artifact/dir belongs to, for per-cell grouping.

    From ``test-reports-<cell>-shard<N>-<run>-<attempt>`` this returns
    ``<cell>`` (e.g. ``win-rtx-sm120-py312-cu130-sm120``). When no shard token
    is present the whole name is treated as its own cell; an empty cell (a bare
    ``test-reports-shard<N>-...`` with no cell segment) renders as "(default)".
    """
    match = _CELL_SHARD.match(name)
    return match.group("cell") if match else name


def _shard_sort_key(label: str) -> tuple[int, object]:
    """Sort numeric shard labels numerically, everything else after, by name."""
    return (0, int(label)) if label.isdigit() else (1, label)


@dataclass
class AggregateResult:
    """Union of per-shard :class:`ScanResult` outcomes, de-duplicated.

    A test that fails in more than one shard (or in repeated rerun reports
    across shards) collapses to a single :class:`Failure` here, with every
    contributing shard recorded in :attr:`shards_by_key`.
    """

    failures: list[Failure] = field(default_factory=list)
    # dedup_key -> set of shard labels the failure was seen in.
    shards_by_key: dict[tuple[str, str, str], set[str]] = field(default_factory=dict)
    passed_keys: set[tuple[str, str, str]] = field(default_factory=set)
    skipped_keys: set[tuple[str, str, str]] = field(default_factory=set)
    scanned_shards: int = 0

    @property
    def _failed_keys(self) -> set[tuple[str, str, str]]:
        return {f.dedup_key for f in self.failures}

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    @property
    def passed_count(self) -> int:
        return len(self.passed_keys - self._failed_keys)

    @property
    def skipped_count(self) -> int:
        return len(self.skipped_keys - self._failed_keys - self.passed_keys)

    @property
    def total_count(self) -> int:
        return self.passed_count + self.failed_count + self.skipped_count


def aggregate_shards(
    shard_dirs: dict[str, Path], *, parse_logs: bool = True
) -> AggregateResult:
    """Collect each shard's reports dir and union the results, de-duplicated.

    ``shard_dirs`` maps a unique name (typically the downloaded artifact
    directory name) to that shard's reports tree. Each shard is scanned
    independently with :func:`collect` - so per-shard rerun reconciliation is
    honoured - and the surviving failures are then merged across shards on
    :attr:`Failure.dedup_key`. The richest record wins (``error`` over
    ``failure``), matching the single-shard sink.
    """
    agg = AggregateResult()
    sink = _FailureSink()
    shards_by_key: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    for name in sorted(shard_dirs):
        label = _shard_label(name)
        result = collect(shard_dirs[name], parse_logs=parse_logs)
        agg.scanned_shards += 1
        agg.passed_keys |= result.passed_keys
        agg.skipped_keys |= result.skipped_keys
        for failure in result.failures:
            sink.add(failure, prefer_error=True)
            shards_by_key[failure.dedup_key].add(label)

    agg.failures = sink.values()
    agg.shards_by_key = dict(shards_by_key)
    return agg


def group_shard_dirs_by_cell(
    shard_dirs: dict[str, Path],
) -> dict[str, dict[str, Path]]:
    """Partition shard dirs into ``{cell: {name: path}}`` by :func:`_cell_label`."""
    groups: dict[str, dict[str, Path]] = defaultdict(dict)
    for name, path in shard_dirs.items():
        groups[_cell_label(name)][name] = path
    return dict(groups)


def aggregate_by_cell(
    shard_dirs: dict[str, Path], *, parse_logs: bool = True
) -> dict[str, AggregateResult]:
    """Aggregate each matrix cell separately (union + dedup within the cell)."""
    groups = group_shard_dirs_by_cell(shard_dirs)
    return {
        cell: aggregate_shards(group, parse_logs=parse_logs)
        for cell, group in groups.items()
    }


def combine_cells(cells: dict[str, AggregateResult]) -> AggregateResult:
    """Fold per-cell aggregates into one overall union across all cells/shards.

    Reuses the already-scanned per-cell :class:`AggregateResult`s (no rescan).
    Failures are de-duplicated across cells on :attr:`Failure.dedup_key`, and
    each contributing shard is recorded as ``<cell>/<shard>`` so the Shards
    column stays unambiguous when the same shard number exists in many cells.
    """
    overall = AggregateResult()
    sink = _FailureSink()
    shards_by_key: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    for cell in sorted(cells):
        agg = cells[cell]
        overall.scanned_shards += agg.scanned_shards
        overall.passed_keys |= agg.passed_keys
        overall.skipped_keys |= agg.skipped_keys
        for failure in agg.failures:
            sink.add(failure, prefer_error=True)
            for shard in agg.shards_by_key.get(failure.dedup_key, set()):
                shards_by_key[failure.dedup_key].add(
                    f"{cell}/{shard}" if cell else shard
                )

    overall.failures = sink.values()
    overall.shards_by_key = dict(shards_by_key)
    return overall


def _agg_totals_line(agg: AggregateResult) -> str:
    """One-line union tally across all shards."""
    return (
        f"**{agg.total_count} distinct test(s) across {agg.scanned_shards} "
        f"shard(s):** {agg.passed_count} passed, "
        f"{agg.failed_count} failed/errored, {agg.skipped_count} skipped."
    )


def _aggregate_body(agg: AggregateResult, max_rows: int) -> list[str]:
    """Markdown lines for one aggregate (totals + de-duplicated failure table).

    Excludes the section heading so the same body can sit under a top-level
    ``## title`` (single aggregate) or a per-cell ``### cell`` sub-heading.
    """
    lines = [_agg_totals_line(agg), ""]

    if not agg.failures:
        lines.append(
            f"All collected tests passed across {agg.scanned_shards} shard(s)."
        )
        return lines

    lines.append(
        f"**{len(agg.failures)} unique failing/errored item(s)** after "
        f"de-duplicating across {agg.scanned_shards} shard(s)."
    )
    lines.append("")
    lines.append("| # | Kind | Source | Test | Shards | Message |")
    lines.append("| --- | --- | --- | --- | --- | --- |")

    ordered = sorted(agg.failures, key=lambda f: (f.module, f.classname, f.name))
    for idx, failure in enumerate(ordered[:max_rows], start=1):
        message = failure.message.replace("|", "\\|") or "-"
        shards = ", ".join(
            sorted(agg.shards_by_key.get(failure.dedup_key, set()), key=_shard_sort_key)
        ) or "-"
        lines.append(
            f"| {idx} | {failure.kind} | {failure.source} "
            f"| `{failure.qualified_name}` | {shards} | {message} |"
        )

    if len(ordered) > max_rows:
        lines.append("")
        lines.append(f"_... and {len(ordered) - max_rows} more (truncated)._ ")

    return lines


def render_aggregate(agg: AggregateResult, title: str, max_rows: int) -> str:
    """Render a single :class:`AggregateResult` as a Markdown section.

    The failure table lists each failing test exactly once (union across
    shards) with a ``Shards`` column showing which shards it was seen in.
    """
    lines = [f"## {title}", ""]

    if agg.scanned_shards == 0:
        lines.append("No shard report directories were found.")
        return "\n".join(lines) + "\n"

    lines.extend(_aggregate_body(agg, max_rows))
    return "\n".join(lines) + "\n"


def render_aggregate_by_cell(
    cells: dict[str, AggregateResult],
    title: str,
    max_rows: int,
    overall: AggregateResult | None = None,
) -> str:
    """Render per-cell aggregate sections under a shared heading.

    Each cell gets its own ``### <cell>`` sub-section with an independent
    union + de-duplication over just that cell's shards. When ``overall`` is
    given (typically only when there is more than one cell), an
    ``### All cells`` section unioning every cell and shard is rendered first.
    """
    lines = [f"## {title}", ""]

    if not cells:
        lines.append("No shard report directories were found.")
        return "\n".join(lines) + "\n"

    if overall is not None:
        lines.append("### All cells")
        lines.append("")
        lines.extend(_aggregate_body(overall, max_rows))
        lines.append("")

    for cell in sorted(cells):
        lines.append(f"### {cell or '(default)'}")
        lines.append("")
        lines.extend(_aggregate_body(cells[cell], max_rows))
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the failure-summary CLI."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reports-dir",
        type=Path,
        default=None,
        help="Directory tree to scan for JUnit *.xml reports and *.log/*.txt run logs.",
    )
    parser.add_argument(
        "--shards-root",
        type=Path,
        default=None,
        help=(
            "Parent directory whose immediate subdirectories are each one "
            "shard's reports tree (e.g. downloaded per-shard artifacts). "
            "Renders one summary per matrix cell, each unioning and "
            "de-duplicating failures across that cell's shards. Mutually "
            "exclusive with --reports-dir."
        ),
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

    if (args.reports_dir is None) == (args.shards_root is None):
        raise SystemExit("error: pass exactly one of --reports-dir or --shards-root")

    if args.shards_root is not None:
        root = args.shards_root
        if not root.is_dir():
            markdown = (
                f"## {args.title}\n\n"
                f"Shards root `{root}` does not exist.\n"
            )
        else:
            shard_dirs = {p.name: p for p in sorted(root.iterdir()) if p.is_dir()}
            cells = aggregate_by_cell(shard_dirs, parse_logs=not args.no_logs)
            overall = combine_cells(cells) if len(cells) > 1 else None
            markdown = render_aggregate_by_cell(
                cells, args.title, args.max_rows, overall=overall
            )
    else:
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
