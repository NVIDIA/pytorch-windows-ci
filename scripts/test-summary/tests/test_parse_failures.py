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

# A flaky test under pytest --reruns with --junit-xml-reruns: the first
# attempt fails and the retry passes, both written as separate <testcase>
# entries (same file/classname/name) in one report. CI scores it a pass.
RERUN_THEN_PASS_REPORT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="suite" tests="2" failures="1" errors="0">
    <testcase classname="test_mod.TestX" name="test_flaky" time="0.5">
      <failure message="deadlock">flaky failure on first attempt</failure>
    </testcase>
    <testcase classname="test_mod.TestX" name="test_flaky" time="0.4"/>
  </testsuite>
</testsuites>
"""

# Whole-process stepcurrent retry: the passing rerun lands in its own report
# while the original failing report is left behind. Matches ``test_fail`` from
# FAILING_REPORT (same empty file / classname / name).
RERUN_PASS_OTHER_FILE = """<?xml version="1.0" encoding="utf-8"?>
<testsuite name="suite" tests="1" failures="0" errors="0">
  <testcase classname="test_mod.TestX" name="test_fail" time="0.1"/>
</testsuite>
"""

# Every rerun attempt failed and no passing <testcase> exists.
CONSISTENT_FAIL_WITH_RERUNS = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="suite" tests="3" failures="3" errors="0">
    <testcase classname="test_x.TestX" name="test_always_fails" time="0.1">
      <failure message="boom 1">attempt 1</failure>
    </testcase>
    <testcase classname="test_x.TestX" name="test_always_fails" time="0.1">
      <failure message="boom 2">attempt 2</failure>
    </testcase>
    <testcase classname="test_x.TestX" name="test_always_fails" time="0.1">
      <failure message="boom 3">attempt 3</failure>
    </testcase>
  </testsuite>
</testsuites>
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


def test_log_runtest_failed_marker_with_progress_token(tmp_path):
    # run_test.py prints an ``<index>/<total>`` token before ``failed!`` for
    # per-file failures (e.g. a cpp-extension build that dies before any
    # JUnit <testcase> is written). The marker must still be recognised.
    _write(
        tmp_path,
        "run.log",
        "cpp_extensions/test_libtorch_agnostic 1/1 failed!\n",
    )

    result = pf.collect(tmp_path)

    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.kind == "error"
    assert failure.name == "test_libtorch_agnostic"


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
# Flaky-rerun reconciliation (fail on one attempt, pass on a later one)
# --------------------------------------------------------------------------
def test_rerun_pass_same_report_not_reported(tmp_path):
    # Failing + passing <testcase> for the same test in one report
    # (pytest --junit-xml-reruns). It passed, so it must not be reported.
    _write(tmp_path, "rerun.xml", RERUN_THEN_PASS_REPORT)

    result = pf.collect(tmp_path)

    assert result.xml_scanned == 1
    assert result.failures == []


def test_rerun_pass_across_reports_not_reported(tmp_path):
    # The passing retry is in a separate report; the earlier failing report
    # is left behind (whole-process stepcurrent retry).
    _write(tmp_path, "attempt1.xml", FAILING_REPORT)
    _write(tmp_path, "attempt2.xml", RERUN_PASS_OTHER_FILE)

    result = pf.collect(tmp_path)

    names = {f.name for f in result.failures}
    # ``test_fail`` passed on rerun and is reconciled away; the genuinely
    # erroring ``test_err`` (never passed) remains.
    assert names == {"test_err"}


def test_consistent_failures_kept(tmp_path):
    # No passing <testcase> anywhere, so the failure stands.
    _write(tmp_path, "fail.xml", CONSISTENT_FAIL_WITH_RERUNS)

    result = pf.collect(tmp_path)

    assert len(result.failures) == 1
    assert result.failures[0].name == "test_always_fails"


def test_log_pass_reconciles_xml_failure(tmp_path):
    # XML records a failing attempt; a later PASSED progress line in the log
    # cancels it (rerun passed, but only the log captured the success).
    _write(tmp_path, "report.xml", AOTI_REPORT)
    _write(
        tmp_path,
        "run.log",
        "inductor/test_torchbind.py::TestTorchbindAOTI::"
        "test_custom_objs_empty_when_no_torchbind PASSED [ 42%]\n",
    )

    result = pf.collect(tmp_path)

    assert result.failures == []


def test_log_pass_reconciles_log_failure(tmp_path):
    # A flaky test seen only in logs: FAILED on the first attempt, PASSED on
    # the retry. The pass wins.
    _write(
        tmp_path,
        "run.log",
        "FAILED test/test_foo.py::TestX::test_bar - transient\n"
        "test/test_foo.py::TestX::test_bar PASSED [ 99%]\n",
    )

    result = pf.collect(tmp_path)

    assert result.failures == []


def test_xdist_result_first_pass_reconciles_log_failure(tmp_path):
    # pytest-xdist prints progress result-FIRST (``[gwN] [ NN%] <RESULT>
    # <nodeid>``). The failing attempt is captured by the result-first
    # failure pattern; the passing rerun must be recognised in the SAME
    # result-first ordering, otherwise it slips through and the (green,
    # rerun-passed) test is wrongly listed.
    _write(
        tmp_path,
        "run.log",
        "[gw3] [ 50%] ERROR test/test_foo.py::TestX::test_bar\n"
        "[gw1] [ 75%] PASSED test/test_foo.py::TestX::test_bar\n",
    )

    result = pf.collect(tmp_path)

    assert result.failures == []


def test_xdist_result_first_pass_reconciles_xml_failure(tmp_path):
    # XML recorded the failing attempt; only an xdist result-first PASSED
    # progress line captured the passing rerun. It must still cancel.
    _write(
        tmp_path,
        "report.xml",
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuite name="suite" tests="1" failures="1" errors="0">\n'
        '  <testcase classname="test_foo.TestX" file="test/test_foo.py"\n'
        '            name="test_bar" time="0.1">\n'
        '    <failure message="boom">tb</failure>\n'
        "  </testcase>\n"
        "</testsuite>\n",
    )
    _write(
        tmp_path,
        "run.log",
        "[gw0] [ 42%] PASSED test/test_foo.py::TestX::test_bar\n",
    )

    result = pf.collect(tmp_path)

    assert result.failures == []


def test_log_error_reconciled_by_xml_skip(tmp_path):
    # A flaky test that errors on a crashed attempt (captured only as an
    # inline ``<nodeid> ERROR:`` log line, no <testcase> written) and is then
    # SKIPPED on the clean rerun (recorded in XML for the same key). CI scores
    # it green, so the stale log error must not be listed.
    _write(
        tmp_path,
        "report.xml",
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuite name="suite" tests="1" failures="0" errors="0" skipped="1">\n'
        '  <testcase classname="TestX" file="sub/test_foo.py"\n'
        '            name="test_bar" time="0.1">\n'
        '    <skipped message="flaky on this platform"/>\n'
        "  </testcase>\n"
        "</testsuite>\n",
    )
    _write(
        tmp_path,
        "run.log",
        "sub\\test_foo.py::TestX::test_bar ERROR: init callback in wrong thread\n",
    )

    result = pf.collect(tmp_path)

    assert result.failures == []
    # It is accounted for as skipped, not failed.
    assert result.failed_count == 0
    assert result.skipped_count == 1


def test_same_basename_different_dirs_do_not_collide(tmp_path):
    # Two distinct tests that share a basename but live in different
    # directories must stay distinct: keying on the basename stem alone would
    # collapse them into one and let one file's pass hide the other's failure.
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuite name="s" tests="2" failures="2" errors="0">\n'
        '  <testcase classname="TestOps" file="test/test_ops.py"\n'
        '            name="test_thing"><failure message="a"/></testcase>\n'
        '  <testcase classname="TestOps" file="functorch/test_ops.py"\n'
        '            name="test_thing"><failure message="b"/></testcase>\n'
        "</testsuite>\n"
    )
    _write(tmp_path, "r.xml", xml)

    result = pf.collect(tmp_path)

    # Both survive as separate failures (distinct module_path).
    assert len(result.failures) == 2
    modules = {f.module_path for f in result.failures}
    assert modules == {"test_ops", "functorch/test_ops"}


def test_pass_in_one_dir_does_not_reconcile_failure_in_another(tmp_path):
    # ``functorch/test_ops.py::test_thing`` passes; the same-basename
    # ``test/test_ops.py::test_thing`` fails. The pass must not cancel the
    # failure, since they are different tests.
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuite name="s" tests="2" failures="1" errors="0">\n'
        '  <testcase classname="TestOps" file="test/test_ops.py"\n'
        '            name="test_thing"><failure message="boom"/></testcase>\n'
        '  <testcase classname="TestOps" file="functorch/test_ops.py"\n'
        '            name="test_thing"/>\n'
        "</testsuite>\n"
    )
    _write(tmp_path, "r.xml", xml)

    result = pf.collect(tmp_path)

    assert {f.module_path for f in result.failures} == {"test_ops"}


def test_earlier_pass_does_not_hide_later_failure(tmp_path):
    # Same test passes in an earlier-timestamped attempt and fails in a
    # later one (a genuine regression across whole-process retries). The
    # later failure must be reported, not masked by the earlier pass.
    early_pass = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuite name="s" timestamp="2026-06-30T05:00:00" failures="0">\n'
        '  <testcase classname="test_mod.TestX" file="test/test_mod.py"\n'
        '            name="test_bar"/>\n'
        "</testsuite>\n"
    )
    later_fail = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuite name="s" timestamp="2026-06-30T06:00:00" failures="1">\n'
        '  <testcase classname="test_mod.TestX" file="test/test_mod.py"\n'
        '            name="test_bar"><failure message="regressed"/></testcase>\n'
        "</testsuite>\n"
    )
    _write(tmp_path, "a_pass.xml", early_pass)
    _write(tmp_path, "b_fail.xml", later_fail)

    result = pf.collect(tmp_path)

    assert {f.name for f in result.failures} == {"test_bar"}


def test_later_pass_reconciles_earlier_failure_by_timestamp(tmp_path):
    # Same test fails first, then passes on a later-timestamped retry. The
    # later pass wins and the failure is reconciled away.
    early_fail = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuite name="s" timestamp="2026-06-30T05:00:00" failures="1">\n'
        '  <testcase classname="test_mod.TestX" file="test/test_mod.py"\n'
        '            name="test_bar"><failure message="flaky"/></testcase>\n'
        "</testsuite>\n"
    )
    later_pass = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuite name="s" timestamp="2026-06-30T06:00:00" failures="0">\n'
        '  <testcase classname="test_mod.TestX" file="test/test_mod.py"\n'
        '            name="test_bar"/>\n'
        "</testsuite>\n"
    )
    _write(tmp_path, "a_fail.xml", early_fail)
    _write(tmp_path, "b_pass.xml", later_pass)

    result = pf.collect(tmp_path)

    assert result.failures == []


def test_skipped_rerun_does_not_cancel_failure(tmp_path):
    # A <skipped> case must not mask a DIFFERENT test's real failure: skip
    # recovery is strictly key-scoped. FAILING_REPORT skips ``test_skip`` while
    # ``test_fail``/``test_err`` (different keys) genuinely fail and remain.
    _write(tmp_path, "fail.xml", FAILING_REPORT)

    result = pf.collect(tmp_path)

    names = {f.name for f in result.failures}
    assert names == {"test_fail", "test_err"}


# --------------------------------------------------------------------------
# Collected / passed / failed / skipped totals
# --------------------------------------------------------------------------
def test_totals_pass_fail_skip(tmp_path):
    # FAILING_REPORT has one failure, one error, and one skipped case.
    _write(tmp_path, "report.xml", FAILING_REPORT)

    result = pf.collect(tmp_path)

    assert result.failed_count == 2
    assert result.skipped_count == 1
    assert result.passed_count == 0
    assert result.total_count == 3


def test_totals_count_passing_report(tmp_path):
    _write(tmp_path, "ok.xml", PASSING_REPORT)

    result = pf.collect(tmp_path)

    assert result.passed_count == 1
    assert result.failed_count == 0
    assert result.skipped_count == 0
    assert result.total_count == 1


def test_totals_rerun_pass_counts_as_passed(tmp_path):
    # Fail-then-pass in one report: reconciled to a pass, so it counts as a
    # single passing test and never as a failure.
    _write(tmp_path, "rerun.xml", RERUN_THEN_PASS_REPORT)

    result = pf.collect(tmp_path)

    assert result.failed_count == 0
    assert result.passed_count == 1
    assert result.skipped_count == 0
    assert result.total_count == 1


def test_totals_dedupe_across_reports(tmp_path):
    # The same passing test in two reports is one distinct passing test.
    _write(tmp_path, "a.xml", PASSING_REPORT)
    _write(tmp_path, "b.xml", PASSING_REPORT)

    result = pf.collect(tmp_path)

    assert result.passed_count == 1
    assert result.total_count == 1


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def test_render_markdown_includes_totals_line(tmp_path):
    _write(tmp_path, "report.xml", FAILING_REPORT)
    result = pf.collect(tmp_path)

    out = pf.render_markdown(result, "Title", 200)

    assert "3 test(s) collected:" in out
    assert "0 passed" in out
    assert "2 failed/errored" in out
    assert "1 skipped" in out


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


def test_render_markdown_header_only_failure_not_green(tmp_path):
    # A header that declares errors but itemizes none (crash signature) with
    # no other failures must NOT render the green "all passed" line.
    _write(tmp_path, "crash.xml", HEADER_ONLY_REPORT)

    result = pf.collect(tmp_path)
    out = pf.render_markdown(result, "Title", 200)

    assert "All collected tests passed" not in out
    assert "Likely crash" in out
    assert "Treat this shard as failed" in out


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
# Cross-shard aggregation
# --------------------------------------------------------------------------
def _shard(root: Path, name: str, files: dict[str, str]) -> Path:
    d = root / name
    d.mkdir(parents=True)
    for fname, content in files.items():
        _write(d, fname, content)
    return d


def test_shard_label_extracts_number():
    assert pf._shard_label("test-reports-cellA-shard3-99-1") == "3"
    assert pf._shard_label("test-reports-shard12-1-1") == "12"
    assert pf._shard_label("no-token-here") == "no-token-here"


def test_aggregate_unions_and_dedups(tmp_path):
    # The same failing tests appear in two shards; the union lists each once,
    # with both shards recorded.
    s1 = _shard(tmp_path, "test-reports-cellA-shard1-9-1", {"r.xml": FAILING_REPORT})
    s2 = _shard(tmp_path, "test-reports-cellA-shard2-9-1", {"r.xml": FAILING_REPORT})

    agg = pf.aggregate_shards({p.name: p for p in (s1, s2)})

    assert agg.scanned_shards == 2
    names = sorted(f.name for f in agg.failures)
    assert names == ["test_err", "test_fail"]  # deduped across the two shards
    key = next(f.dedup_key for f in agg.failures if f.name == "test_fail")
    assert agg.shards_by_key[key] == {"1", "2"}
    assert agg.failed_count == 2
    assert agg.skipped_count == 1  # test_skip, unioned once


def test_aggregate_distinct_failures_and_passes(tmp_path):
    # Shard 1 fails; shard 2 only passes. Failures come from shard 1 alone,
    # and shard 2's pass contributes to the passed tally.
    s1 = _shard(tmp_path, "test-reports-shard1-9-1", {"r.xml": FAILING_REPORT})
    s2 = _shard(tmp_path, "test-reports-shard2-9-1", {"r.xml": PASSING_REPORT})

    agg = pf.aggregate_shards({p.name: p for p in (s1, s2)})

    assert agg.failed_count == 2
    key = next(f.dedup_key for f in agg.failures if f.name == "test_fail")
    assert agg.shards_by_key[key] == {"1"}
    assert agg.passed_count == 1  # test_passes from shard 2


def test_render_aggregate_shows_shards_column(tmp_path):
    s1 = _shard(tmp_path, "test-reports-shard1-9-1", {"r.xml": FAILING_REPORT})
    s2 = _shard(tmp_path, "test-reports-shard2-9-1", {"r.xml": FAILING_REPORT})
    agg = pf.aggregate_shards({p.name: p for p in (s1, s2)})

    out = pf.render_aggregate(agg, "Aggregate", 200)

    assert "distinct test(s) across 2 shard(s)" in out
    assert "2 unique failing/errored item(s)" in out
    assert "| Shards |" in out
    assert "1, 2" in out  # a failure seen in both shards


def test_render_aggregate_no_shards():
    out = pf.render_aggregate(pf.AggregateResult(), "Aggregate", 200)
    assert "No shard report directories were found." in out


def test_render_aggregate_all_passed(tmp_path):
    s1 = _shard(tmp_path, "test-reports-shard1-9-1", {"r.xml": PASSING_REPORT})
    agg = pf.aggregate_shards({s1.name: s1})
    out = pf.render_aggregate(agg, "Aggregate", 200)
    assert "All collected tests passed across 1 shard(s)." in out


# --------------------------------------------------------------------------
# Per-cell aggregation
# --------------------------------------------------------------------------
def test_cell_label_extraction():
    assert (
        pf._cell_label("test-reports-win-rtx-sm89-py312-cu130-sm89-shard1-99-1")
        == "win-rtx-sm89-py312-cu130-sm89"
    )
    assert (
        pf._cell_label("test-reports-win-rtx-sm120-py312-cu132-sm120-shard5-99-1")
        == "win-rtx-sm120-py312-cu132-sm120"
    )
    # No cell segment -> empty (renders as "(default)").
    assert pf._cell_label("test-reports-shard3-99-1") == ""


def test_group_shard_dirs_by_cell(tmp_path):
    names = [
        "test-reports-cellA-shard1-9-1",
        "test-reports-cellA-shard2-9-1",
        "test-reports-cellB-shard1-9-1",
    ]
    groups = pf.group_shard_dirs_by_cell({n: tmp_path / n for n in names})
    assert set(groups) == {"cellA", "cellB"}
    assert len(groups["cellA"]) == 2
    assert len(groups["cellB"]) == 1


def test_aggregate_by_cell_separates_cells(tmp_path):
    a1 = _shard(tmp_path, "test-reports-cellA-shard1-9-1", {"r.xml": FAILING_REPORT})
    a2 = _shard(tmp_path, "test-reports-cellA-shard2-9-1", {"r.xml": FAILING_REPORT})
    b1 = _shard(tmp_path, "test-reports-cellB-shard1-9-1", {"r.xml": AOTI_REPORT})

    cells = pf.aggregate_by_cell({p.name: p for p in (a1, a2, b1)})

    assert set(cells) == {"cellA", "cellB"}
    # cellA: test_fail deduped across its two shards.
    key = next(f.dedup_key for f in cells["cellA"].failures if f.name == "test_fail")
    assert cells["cellA"].shards_by_key[key] == {"1", "2"}
    # cellB: only its own single failure, from shard 1.
    assert cells["cellB"].failed_count == 1
    b_key = cells["cellB"].failures[0].dedup_key
    assert cells["cellB"].shards_by_key[b_key] == {"1"}


def test_render_aggregate_by_cell_has_per_cell_sections(tmp_path):
    a1 = _shard(tmp_path, "test-reports-cellA-shard1-9-1", {"r.xml": FAILING_REPORT})
    b1 = _shard(tmp_path, "test-reports-cellB-shard1-9-1", {"r.xml": AOTI_REPORT})
    cells = pf.aggregate_by_cell({p.name: p for p in (a1, b1)})

    out = pf.render_aggregate_by_cell(cells, "Aggregate", 200)

    assert "## Aggregate" in out
    assert "### cellA" in out
    assert "### cellB" in out
    assert "| Shards |" in out


def test_render_aggregate_by_cell_empty():
    out = pf.render_aggregate_by_cell({}, "Aggregate", 200)
    assert "No shard report directories were found." in out


def test_combine_cells_unions_across_cells(tmp_path):
    # Same failing test appears in two different cells.
    a1 = _shard(tmp_path, "test-reports-cellA-shard1-9-1", {"r.xml": FAILING_REPORT})
    b1 = _shard(tmp_path, "test-reports-cellB-shard2-9-1", {"r.xml": FAILING_REPORT})
    cells = pf.aggregate_by_cell({p.name: p for p in (a1, b1)})

    overall = pf.combine_cells(cells)

    assert overall.scanned_shards == 2
    # test_fail is deduped to a single row across both cells.
    key = next(f.dedup_key for f in overall.failures if f.name == "test_fail")
    assert overall.shards_by_key[key] == {"cellA/1", "cellB/2"}


def test_render_aggregate_by_cell_includes_overall(tmp_path):
    a1 = _shard(tmp_path, "test-reports-cellA-shard1-9-1", {"r.xml": FAILING_REPORT})
    b1 = _shard(tmp_path, "test-reports-cellB-shard1-9-1", {"r.xml": AOTI_REPORT})
    cells = pf.aggregate_by_cell({p.name: p for p in (a1, b1)})
    overall = pf.combine_cells(cells)

    out = pf.render_aggregate_by_cell(cells, "Aggregate", 200, overall=overall)

    assert "### All cells" in out
    assert "### cellA" in out
    assert "### cellB" in out
    # "All cells" is rendered before the individual cells.
    assert out.index("### All cells") < out.index("### cellA")


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def test_main_missing_dir_writes_notice(tmp_path, capsys):
    rc = pf.main(["--reports-dir", str(tmp_path / "nope"), "--title", "T"])
    captured = capsys.readouterr().out
    assert rc == 0
    assert "does not exist" in captured


def test_main_shards_root_renders_aggregate(tmp_path):
    root = tmp_path / "all-reports"
    _shard(root, "test-reports-shard1-9-1", {"r.xml": FAILING_REPORT})
    _shard(root, "test-reports-shard2-9-1", {"r.xml": FAILING_REPORT})
    out_file = tmp_path / "summary.md"

    rc = pf.main(
        ["--shards-root", str(root), "--title", "Aggregate", "--output", str(out_file)]
    )

    assert rc == 0
    content = out_file.read_text(encoding="utf-8")
    assert "unique failing/errored item(s)" in content
    assert "1, 2" in content


def test_main_requires_exactly_one_source(tmp_path):
    # Neither source -> error.
    with pytest.raises(SystemExit):
        pf.main(["--title", "T"])
    # Both sources -> error.
    with pytest.raises(SystemExit):
        pf.main(["--reports-dir", str(tmp_path), "--shards-root", str(tmp_path)])


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
