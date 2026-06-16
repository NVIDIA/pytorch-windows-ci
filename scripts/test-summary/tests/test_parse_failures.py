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
<testsuite name="suite" tests="1" failures="0" errors="0">
  <testcase classname="test_ok.TestA" name="test_passes" time="0.1"/>
</testsuite>
"""

FAILING_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="suite" tests="3" failures="1" errors="1">
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

AOTI_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="suite" tests="1" failures="1" errors="0">
    <testcase classname="TestTorchbindAOTI" file="inductor/test_torchbind.py"
              name="test_custom_objs_empty_when_no_torchbind" time="0.3">
      <failure message="boom">tb</failure>
    </testcase>
  </testsuite>
</testsuites>
"""

# A testsuite that *declares* a failure in its header but itemizes no
# failing case - the JUnit signature of a crash that took the file down.
HEADER_ONLY_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="test_crash" tests="4" failures="0" errors="2">
  <testcase classname="test_crash.TestC" name="test_ok" time="0.1"/>
</testsuite>
"""

# Collection/import error emitted at the suite level (no enclosing case).
SUITE_LEVEL_ERROR = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="test_import" tests="0" failures="0" errors="1">
  <error message="ImportError: no module named foo">Traceback...</error>
</testsuite>
"""


def _write(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# JUnit XML parsing
# --------------------------------------------------------------------------
def test_collect_finds_failures_and_errors(tmp_path):
    _write(tmp_path, "report.xml", FAILING_REPORT)

    result = pf.collect(tmp_path)

    assert result.xml_scanned == 1
    assert result.xml_unparsable == 0
    kinds = {(f.name, f.kind) for f in result.failures}
    assert kinds == {("test_fail", "failure"), ("test_err", "error")}


def test_collect_ignores_passing_and_skipped(tmp_path):
    _write(tmp_path, "ok.xml", PASSING_REPORT)

    result = pf.collect(tmp_path)

    assert result.xml_scanned == 1
    assert result.failures == []


def test_collect_dedupes_reruns(tmp_path):
    _write(tmp_path, "a.xml", FAILING_REPORT)
    _write(tmp_path, "b.xml", FAILING_REPORT)

    result = pf.collect(tmp_path)

    assert result.xml_scanned == 2
    assert len(result.failures) == 2  # collapsed across the two reports


def test_collect_recurses_subdirectories(tmp_path):
    nested = tmp_path / "test-reports" / "python-unittest"
    nested.mkdir(parents=True)
    _write(nested, "deep.xml", FAILING_REPORT)

    result = pf.collect(tmp_path)

    assert result.xml_scanned == 1
    assert len(result.failures) == 2


def test_unparsable_xml_surfaced_as_crash(tmp_path):
    _write(tmp_path, "good.xml", FAILING_REPORT)
    _write(tmp_path, "bad.xml", "<not valid xml")

    result = pf.collect(tmp_path)

    assert result.xml_scanned == 2
    assert result.xml_unparsable == 1
    crashes = [f for f in result.failures if f.kind == "crash"]
    assert len(crashes) == 1
    assert crashes[0].source == "report"
    assert crashes[0].name == "bad.xml"
    # The two real failures plus the crash row.
    assert len(result.failures) == 3


def test_suite_level_error_collected(tmp_path):
    _write(tmp_path, "import.xml", SUITE_LEVEL_ERROR)

    result = pf.collect(tmp_path)

    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.kind == "error"
    assert failure.name == "test_import"
    assert "ImportError" in failure.message


def test_header_declares_more_than_itemized(tmp_path):
    _write(tmp_path, "crash.xml", HEADER_ONLY_REPORT)

    result = pf.collect(tmp_path)

    # Header claims 2 errors; zero failing cases were itemized.
    assert result.reported_failures == 2
    assert result.xml_itemized == 0
    assert result.missing_itemization == 2


# --------------------------------------------------------------------------
# Module / qualified-name helpers
# --------------------------------------------------------------------------
def test_module_from_file_attribute(tmp_path):
    _write(tmp_path, "aoti.xml", AOTI_REPORT)

    result = pf.collect(tmp_path)

    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.module == "test_torchbind"
    assert failure.qualified_name == (
        "test_torchbind::TestTorchbindAOTI::"
        "test_custom_objs_empty_when_no_torchbind"
    )


def test_module_from_dotted_classname():
    failure = pf.Failure("test_mod.TestX", "test_a", "failure", "")
    assert failure.module == "test_mod"
    assert failure.qualified_name == "test_mod.TestX::test_a"


def test_module_absent_when_no_file_or_dot():
    failure = pf.Failure("TestX", "test_a", "failure", "")
    assert failure.module == ""
    assert failure.qualified_name == "TestX::test_a"


# --------------------------------------------------------------------------
# Log parsing
# --------------------------------------------------------------------------
def test_log_short_summary_form(tmp_path):
    _write(
        tmp_path,
        "run.log",
        "FAILED test/test_foo.py::TestX::test_bar - AssertionError: nope\n",
    )

    result = pf.collect(tmp_path)

    assert result.logs_scanned == 1
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.source == "log"
    assert failure.kind == "failure"
    assert failure.module == "test_foo"
    assert failure._class_leaf == "TestX"
    assert failure.name == "test_bar"
    assert "AssertionError" in failure.message


def test_log_error_summary_form(tmp_path):
    _write(tmp_path, "run.log", "ERROR test/test_baz.py - ImportError: boom\n")

    result = pf.collect(tmp_path)

    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.kind == "error"
    # No ``::`` segment -> file-level error named after the module.
    assert failure.name == "test_baz"


def test_log_inline_progress_form(tmp_path):
    _write(
        tmp_path,
        "run.log",
        "test/test_qux.py::TestQ::test_z FAILED [ 73%]\n",
    )

    result = pf.collect(tmp_path)

    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.kind == "failure"
    assert failure.name == "test_z"


def test_log_tolerates_xdist_prefix_and_ansi(tmp_path):
    line = "[gw3] \x1b[31mFAILED\x1b[0m test/test_p.py::test_q - boom\n"
    _write(tmp_path, "run.log", line)

    result = pf.collect(tmp_path)

    assert len(result.failures) == 1
    assert result.failures[0].name == "test_q"


def test_log_runtest_failed_marker(tmp_path):
    _write(tmp_path, "run.log", "test_distributed failed!\n")

    result = pf.collect(tmp_path)

    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.kind == "error"
    assert failure.name == "test_distributed"


def test_log_ignores_generic_failed_prose(tmp_path):
    # "assertion failed" must not be mistaken for a failed test file.
    _write(tmp_path, "run.log", "assertion failed\nbuild step failed!\n")

    result = pf.collect(tmp_path)

    assert result.failures == []


def test_no_logs_flag_skips_log_parsing(tmp_path):
    _write(tmp_path, "run.log", "FAILED test/test_foo.py::test_bar - boom\n")

    result = pf.collect(tmp_path, parse_logs=False)

    assert result.logs_scanned == 0
    assert result.failures == []


# --------------------------------------------------------------------------
# Cross-source de-duplication
# --------------------------------------------------------------------------
def test_xml_and_log_failures_merge(tmp_path):
    # XML reports test_mod.TestX::test_fail and ::test_err; the log repeats
    # test_fail (same test) and adds a brand-new one.
    _write(tmp_path, "report.xml", FAILING_REPORT)
    _write(
        tmp_path,
        "run.log",
        "FAILED test/test_mod.py::TestX::test_fail - dupe\n"
        "FAILED test/test_other.py::TestY::test_new - fresh\n",
    )

    result = pf.collect(tmp_path)

    names = sorted(f.name for f in result.failures)
    assert names == ["test_err", "test_fail", "test_new"]
    # The shared test keeps the richer XML record, not the log dupe.
    shared = next(f for f in result.failures if f.name == "test_fail")
    assert shared.source == "xml"
    assert shared.message == "assert 1 == 2"


def test_log_function_style_matches_xml_module_classname(tmp_path):
    # XML classname equal to the module (function-style test) must collapse
    # with the log nodeid that carries no class segment.
    xml = (
        '<?xml version="1.0"?><testsuite name="s" failures="1">'
        '<testcase classname="test_fn" name="test_top" file="test_fn.py">'
        '<failure message="x"/></testcase></testsuite>'
    )
    _write(tmp_path, "r.xml", xml)
    _write(tmp_path, "run.log", "FAILED test/test_fn.py::test_top - dup\n")

    result = pf.collect(tmp_path)

    assert len(result.failures) == 1
    assert result.failures[0].source == "xml"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def test_render_markdown_includes_module(tmp_path):
    _write(tmp_path, "aoti.xml", AOTI_REPORT)
    result = pf.collect(tmp_path)

    out = pf.render_markdown(result, "Title", 200)

    assert "test_torchbind::TestTorchbindAOTI" in out
    assert "Source" in out


def test_render_markdown_no_reports():
    out = pf.render_markdown(pf.ScanResult(), "Title", 200)
    assert "No test report XML files or run logs were found." in out


def test_render_markdown_all_passed():
    result = pf.ScanResult(xml_scanned=5, logs_scanned=1)
    out = pf.render_markdown(result, "Title", 200)
    assert "All collected tests passed" in out


def test_render_markdown_notes_crash_and_missing(tmp_path):
    _write(tmp_path, "bad.xml", "<broken")
    _write(tmp_path, "crash.xml", HEADER_ONLY_REPORT)

    result = pf.collect(tmp_path)
    out = pf.render_markdown(result, "Title", 200)

    assert "crash row" in out
    assert "not itemized" in out


def test_render_markdown_lists_failures_and_escapes_pipe():
    result = pf.ScanResult(
        failures=[pf.Failure("Cls", "test_a", "failure", "a | b")],
        xml_scanned=1,
    )
    out = pf.render_markdown(result, "Title", 200)
    assert "failing/errored item(s)" in out
    assert "`Cls::test_a`" in out
    assert "a \\| b" in out


def test_render_markdown_truncates_rows():
    result = pf.ScanResult(
        failures=[pf.Failure("Cls", f"test_{i}", "failure", "") for i in range(5)],
        xml_scanned=1,
    )
    out = pf.render_markdown(result, "Title", 2)
    assert "and 3 more (truncated)" in out


def test_first_line_truncates():
    assert pf._first_line(None) == ""
    assert pf._first_line("   ") == ""
    assert pf._first_line("first\nsecond") == "first"
    long = "x" * 250
    out = pf._first_line(long, limit=10)
    assert out == "x" * 10 + "..."


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
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


def test_main_no_logs_flag(tmp_path):
    # A passing XML plus a log that reports a failure: with --no-logs the
    # log must be ignored, leaving an all-passed summary.
    _write(tmp_path, "ok.xml", PASSING_REPORT)
    _write(tmp_path, "run.log", "FAILED test/test_foo.py::test_bar - boom\n")
    out_file = tmp_path / "summary.md"
    rc = pf.main(
        ["--reports-dir", str(tmp_path), "--no-logs", "--output", str(out_file)]
    )
    assert rc == 0
    assert "All collected tests passed" in out_file.read_text(encoding="utf-8")
