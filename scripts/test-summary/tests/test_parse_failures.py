# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``parse_failures.py``."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import parse_failures as pf  # noqa: E402

PASSING_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="suite" tests="1">
  <testcase classname="test_ok.TestA" name="test_passes" time="0.1"/>
</testsuite>
"""

FAILING_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="suite" tests="3">
    <testcase classname="test_mod.TestX" name="test_fail" time="0.2">
      <failure message="assert 1 == 2">long\ntraceback\nhere</failure>
    </testcase>
    <testcase classname="test_mod.TestX" name="test_err" time="0.0">
      <error message="RuntimeError: boom"/>
    </testcase>
    <testcase classname="test_mod.TestX" name="test_skip" time="0.0">
      <skipped message="not on this platform"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def _write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def test_collect_failures_finds_failures_and_errors(tmp_path):
    _write(tmp_path, "report.xml", FAILING_REPORT)

    failures, scanned, unparsable = pf.collect_failures(tmp_path)

    assert scanned == 1
    assert unparsable == 0
    kinds = {(f.name, f.kind) for f in failures}
    assert kinds == {("test_fail", "failure"), ("test_err", "error")}


def test_collect_failures_ignores_passing_and_skipped(tmp_path):
    _write(tmp_path, "ok.xml", PASSING_REPORT)

    failures, scanned, unparsable = pf.collect_failures(tmp_path)

    assert scanned == 1
    assert failures == []


def test_collect_failures_dedupes_reruns(tmp_path):
    _write(tmp_path, "a.xml", FAILING_REPORT)
    _write(tmp_path, "b.xml", FAILING_REPORT)

    failures, scanned, _ = pf.collect_failures(tmp_path)

    assert scanned == 2
    assert len(failures) == 2  # collapsed across the two identical reports


def test_collect_failures_recurses_subdirectories(tmp_path):
    nested = tmp_path / "test-reports" / "python-unittest"
    nested.mkdir(parents=True)
    _write(nested, "deep.xml", FAILING_REPORT)

    failures, scanned, _ = pf.collect_failures(tmp_path)

    assert scanned == 1
    assert len(failures) == 2


def test_collect_failures_skips_unparsable(tmp_path):
    _write(tmp_path, "good.xml", FAILING_REPORT)
    _write(tmp_path, "bad.xml", "<not valid xml")

    failures, scanned, unparsable = pf.collect_failures(tmp_path)

    assert scanned == 2
    assert unparsable == 1
    assert len(failures) == 2


def test_first_line_truncates():
    assert pf._first_line(None) == ""
    assert pf._first_line("   ") == ""
    assert pf._first_line("first\nsecond") == "first"
    long = "x" * 250
    out = pf._first_line(long, limit=10)
    assert out == "x" * 10 + "..."


def test_render_markdown_no_reports():
    out = pf.render_markdown([], 0, 0, "Title", 200)
    assert "No test report XML files were found." in out


def test_render_markdown_all_passed():
    out = pf.render_markdown([], 5, 0, "Title", 200)
    assert "All collected tests passed" in out


def test_render_markdown_lists_failures_and_escapes_pipe():
    failures = [pf.Failure("Cls", "test_a", "failure", "a | b")]
    out = pf.render_markdown(failures, 1, 0, "Title", 200)
    assert "1 failing test(s)" in out
    assert "`Cls::test_a`" in out
    assert "a \\| b" in out


def test_render_markdown_truncates_rows():
    failures = [
        pf.Failure("Cls", f"test_{i}", "failure", "") for i in range(5)
    ]
    out = pf.render_markdown(failures, 1, 0, "Title", max_rows=2)
    assert "and 3 more (truncated)" in out


def test_main_missing_dir_writes_notice(tmp_path, capsys):
    rc = pf.main(["--reports-dir", str(tmp_path / "nope"), "--title", "T"])
    captured = capsys.readouterr().out
    assert rc == 0
    assert "does not exist" in captured


def test_main_writes_to_output_file(tmp_path):
    _write(tmp_path, "r.xml", FAILING_REPORT)
    out_file = tmp_path / "summary.md"
    rc = pf.main(
        [
            "--reports-dir",
            str(tmp_path),
            "--title",
            "Shard 1",
            "--output",
            str(out_file),
        ]
    )
    assert rc == 0
    content = out_file.read_text(encoding="utf-8")
    assert "Shard 1" in content
    assert "test_fail" in content
