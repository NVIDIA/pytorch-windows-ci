# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``build_test_results.py``.

The emphasis here is the fail-closed contract: every way a cell can produce
incomplete evidence must still come out as ``failure``, because a wrong green
row on the upstream HUD is far more damaging than a spurious red one.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_test_results as btr  # noqa: E402


def _suite(cases: str, *, failures: int = 0, errors: int = 0) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<testsuite name="suite" failures="{failures}" errors="{errors}">\n'
        f"{cases}\n"
        "</testsuite>\n"
    )


def _passing(name: str, classname: str = "test_mod.TestA") -> str:
    return f'  <testcase classname="{classname}" name="{name}" file="test_mod.py"/>'


def _failing(name: str, classname: str = "test_mod.TestA") -> str:
    return (
        f'  <testcase classname="{classname}" name="{name}" file="test_mod.py">'
        '<failure message="boom">traceback</failure></testcase>'
    )


def _skipped(name: str, classname: str = "test_mod.TestA") -> str:
    return (
        f'  <testcase classname="{classname}" name="{name}" file="test_mod.py">'
        "<skipped/></testcase>"
    )


# XML that stops mid-element, as a report does when the process writing it is
# killed by a timeout, a crash or a cancellation.
_TRUNCATED_XML = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<testsuite name="suite" failures="0">\n'
    '  <testcase classname="test_mod.TestA" name="a" file="test_mod.py"/>\n'
    '  <testcase classname="test_mod.TestA" name="b'
)


def _write_shard(root: Path, shard: str, xml: str) -> Path:
    shard_dir = root / shard
    shard_dir.mkdir(parents=True, exist_ok=True)
    (shard_dir / "report.xml").write_text(xml, encoding="utf-8")
    return shard_dir


class TestFindShardDirs:
    def test_lists_immediate_subdirectories(self, tmp_path: Path) -> None:
        _write_shard(tmp_path, "shard1", _suite(_passing("a")))
        _write_shard(tmp_path, "shard2", _suite(_passing("b")))
        (tmp_path / "loose.txt").write_text("ignored", encoding="utf-8")

        assert sorted(btr.find_shard_dirs(tmp_path)) == ["shard1", "shard2"]

    def test_missing_root_yields_nothing(self, tmp_path: Path) -> None:
        assert btr.find_shard_dirs(tmp_path / "absent") == {}


class TestCollectCell:
    def test_unions_counts_across_shards(self, tmp_path: Path) -> None:
        _write_shard(tmp_path, "shard1", _suite(_passing("a") + "\n" + _passing("b")))
        _write_shard(
            tmp_path,
            "shard2",
            _suite(_failing("c") + "\n" + _skipped("d"), failures=1),
        )

        results = btr.collect_cell(btr.find_shard_dirs(tmp_path))

        assert (results.passed, results.failed, results.skipped) == (2, 1, 1)
        assert results.total == 4
        assert results.shards == 2
        assert results.conclusion == "failure"

    def test_counts_a_test_once_when_several_shards_report_it(
        self, tmp_path: Path
    ) -> None:
        _write_shard(tmp_path, "shard1", _suite(_failing("flaky"), failures=1))
        _write_shard(tmp_path, "shard2", _suite(_failing("flaky"), failures=1))

        results = btr.collect_cell(btr.find_shard_dirs(tmp_path))

        assert results.failed == 1
        assert results.failing_tests == ["test_mod.TestA::flaky"]

    def test_failure_in_one_shard_outranks_a_pass_in_another(
        self, tmp_path: Path
    ) -> None:
        _write_shard(tmp_path, "shard1", _suite(_passing("same")))
        _write_shard(tmp_path, "shard2", _suite(_failing("same"), failures=1))

        results = btr.collect_cell(btr.find_shard_dirs(tmp_path))

        assert (results.passed, results.failed) == (0, 1)

    def test_all_green_is_success(self, tmp_path: Path) -> None:
        _write_shard(tmp_path, "shard1", _suite(_passing("a")))
        _write_shard(tmp_path, "shard2", _suite(_passing("b") + "\n" + _skipped("c")))

        results = btr.collect_cell(btr.find_shard_dirs(tmp_path))

        assert results.conclusion == "success"
        assert (results.passed, results.failed, results.skipped) == (2, 0, 1)

    def test_no_shards_is_failure_not_empty_success(self, tmp_path: Path) -> None:
        results = btr.collect_cell({})

        assert results.shards == 0
        assert results.total == 0
        assert results.conclusion == "failure"

    def test_a_missing_shard_fails_an_otherwise_green_cell(
        self, tmp_path: Path
    ) -> None:
        # Four green shards out of five. The absent shard timed out, crashed or
        # was cancelled before uploading, and `if-no-files-found: warn` means
        # its job can still be green - so the shortfall is the only evidence.
        for shard in range(1, 5):
            _write_shard(tmp_path, f"shard{shard}", _suite(_passing(f"a{shard}")))

        results = btr.collect_cell(btr.find_shard_dirs(tmp_path), expected_shards=5)

        assert (results.shards, results.missing_shards) == (4, 1)
        assert results.failed == 0
        assert results.conclusion == "failure"

    def test_no_expectation_declared_leaves_the_check_off(
        self, tmp_path: Path
    ) -> None:
        _write_shard(tmp_path, "shard1", _suite(_passing("a")))

        results = btr.collect_cell(btr.find_shard_dirs(tmp_path))

        assert results.missing_shards == 0
        assert results.conclusion == "success"

    def test_extra_shards_are_not_a_failure(self, tmp_path: Path) -> None:
        for shard in range(1, 7):
            _write_shard(tmp_path, f"shard{shard}", _suite(_passing(f"a{shard}")))

        results = btr.collect_cell(btr.find_shard_dirs(tmp_path), expected_shards=5)

        assert results.missing_shards == 0
        assert results.conclusion == "success"

    def test_unitemised_header_failures_are_treated_as_a_crash(
        self, tmp_path: Path
    ) -> None:
        _write_shard(tmp_path, "shard1", _suite(_passing("a"), failures=5))

        results = btr.collect_cell(btr.find_shard_dirs(tmp_path))

        assert results.crash_signals == 5
        assert results.failed == 0
        assert results.conclusion == "failure"

    def test_a_truncated_report_fails_the_cell(self, tmp_path: Path) -> None:
        # Every expected shard present, nothing missing, no header failure
        # count - the only evidence of trouble is XML that stops mid-element,
        # the usual signature of a process dying part-way through writing it.
        _write_shard(tmp_path, "shard1", _TRUNCATED_XML)

        results = btr.collect_cell(btr.find_shard_dirs(tmp_path), expected_shards=1)

        assert results.unparsable_reports == 1
        assert results.missing_shards == 0
        assert results.conclusion == "failure"

    def test_a_truncated_report_is_not_masked_by_a_green_sibling(
        self, tmp_path: Path
    ) -> None:
        # The dangerous shape: one readable report full of passes, one corrupt.
        # Summing per-file outcomes would call this green.
        shard = _write_shard(tmp_path, "shard1", _suite(_passing("a")))
        (shard / "truncated.xml").write_text(_TRUNCATED_XML, encoding="utf-8")

        results = btr.collect_cell(btr.find_shard_dirs(tmp_path), expected_shards=1)

        assert results.unparsable_reports == 1
        assert results.passed >= 1
        assert results.conclusion == "failure"

    def test_unparsable_alone_is_enough_without_a_synthetic_failure(self) -> None:
        # Locks the predicate itself rather than the path that feeds it. If
        # `parse_failures` ever stops emitting a crash row for unreadable XML,
        # the counter alone still has to keep the cell red.
        results = btr.CellResults(shards=1, expected_shards=1, passed=10)
        assert results.conclusion == "success"

        results.unparsable_reports = 1
        assert results.conclusion == "failure"


class TestAsTestResults:
    def test_shape_matches_the_relay_contract(self, tmp_path: Path) -> None:
        _write_shard(
            tmp_path,
            "shard1",
            _suite(
                "\n".join([_passing("a"), _failing("b"), _skipped("c")]), failures=1
            ),
        )

        payload = btr.collect_cell(btr.find_shard_dirs(tmp_path)).as_test_results()

        assert payload == {"passed": 1, "failed": 1, "skipped": 1, "total": 3}


class TestMain:
    def _run(self, tmp_path: Path, shards_root: Path) -> dict[str, str]:
        output = tmp_path / "out.env"
        assert (
            btr.main(
                [
                    "--shards-root",
                    str(shards_root),
                    "--github-output",
                    str(output),
                ]
            )
            == 0
        )
        return dict(
            line.split("=", 1)
            for line in output.read_text(encoding="utf-8").splitlines()
        )

    def test_emits_json_test_results(self, tmp_path: Path) -> None:
        root = tmp_path / "reports"
        _write_shard(root, "shard1", _suite(_passing("a") + "\n" + _passing("b")))

        values = self._run(tmp_path, root)

        assert json.loads(values["test-results"]) == {
            "passed": 2,
            "failed": 0,
            "skipped": 0,
            "total": 2,
        }
        assert values["conclusion"] == "success"

    def test_reports_failure_when_nothing_was_downloaded(self, tmp_path: Path) -> None:
        values = self._run(tmp_path, tmp_path / "never-created")

        assert values["conclusion"] == "failure"
        assert values["shards"] == "0"

    def test_exits_zero_even_when_the_cell_failed(self, tmp_path: Path) -> None:
        root = tmp_path / "reports"
        _write_shard(root, "shard1", _suite(_failing("a"), failures=1))

        # A non-zero exit would kill the reporting job before the callback
        # went out, which is the one outcome this pipeline cannot tolerate.
        assert btr.main(["--shards-root", str(root)]) == 0

    def test_summary_names_the_failing_tests(self, tmp_path: Path) -> None:
        root = tmp_path / "reports"
        _write_shard(root, "shard1", _suite(_failing("broken"), failures=1))
        summary = tmp_path / "summary.md"

        btr.main(["--shards-root", str(root), "--summary", str(summary)])

        text = summary.read_text(encoding="utf-8")
        assert "broken" in text
        assert "| conclusion | `failure` |" in text

    def test_summary_explains_an_empty_download(self, tmp_path: Path) -> None:
        summary = tmp_path / "summary.md"

        btr.main(["--shards-root", str(tmp_path / "absent"), "--summary", str(summary)])

        assert "No shard report directories were found" in summary.read_text(
            encoding="utf-8"
        )

    def test_summary_calls_out_a_short_shard_count(self, tmp_path: Path) -> None:
        root = tmp_path / "reports"
        _write_shard(root, "shard1", _suite(_passing("a")))
        summary = tmp_path / "summary.md"

        btr.main(
            [
                "--shards-root",
                str(root),
                "--expected-shards",
                "5",
                "--summary",
                str(summary),
            ]
        )

        text = summary.read_text(encoding="utf-8")
        assert "4 of 5 shards left no reports" in text
        assert "| conclusion | `failure` |" in text

    def test_missing_shard_count_is_exposed_as_an_output(self, tmp_path: Path) -> None:
        root = tmp_path / "reports"
        _write_shard(root, "shard1", _suite(_passing("a")))
        output = tmp_path / "out.env"

        btr.main(
            [
                "--shards-root",
                str(root),
                "--expected-shards",
                "5",
                "--github-output",
                str(output),
            ]
        )

        values = dict(
            line.split("=", 1)
            for line in output.read_text(encoding="utf-8").splitlines()
        )
        assert values["missing-shards"] == "4"
        assert values["conclusion"] == "failure"


class TestCollectGroups:
    """Combining several cells into one row.

    The cells run the same test suite, so the interesting property is that
    counts are *summed* rather than unioned on test identity -- otherwise five
    Python versions running one test would report as one result.
    """

    GROUP_RE = re.compile(r"win-woa-arm64-(?P<group>[^-]+)-arm64-shard")

    def _version(self, root: Path, label: str, shard: int, xml: str) -> Path:
        return _write_shard(
            root, f"test-reports-win-woa-arm64-{label}-arm64-shard{shard}-9-1", xml
        )

    def test_counts_are_summed_not_unioned(self, tmp_path: Path) -> None:
        # Same test name in both versions: unioning would report 1, not 2.
        for label in ("py311", "py312"):
            self._version(tmp_path, label, 1, _suite(_passing("test_same")))

        results = btr.collect_groups(
            btr.find_shard_dirs(tmp_path), self.GROUP_RE, expected_shards=1
        )

        assert results.groups == 2
        assert results.passed == 2
        assert results.conclusion == "success"

    def test_failing_tests_are_tagged_with_their_version(self, tmp_path: Path) -> None:
        self._version(tmp_path, "py311", 1, _suite(_failing("test_x"), failures=1))
        self._version(tmp_path, "py312", 1, _suite(_passing("test_x")))

        results = btr.collect_groups(
            btr.find_shard_dirs(tmp_path), self.GROUP_RE, expected_shards=1
        )

        # Failing on one version and passing on another must stay attributable
        # and must not be cancelled out by the version that passed.
        assert results.failed == 1
        assert results.passed == 1
        assert any(name.startswith("py311: ") for name in results.failing_tests)
        assert results.conclusion == "failure"

    def test_a_whole_missing_version_fails_the_row(self, tmp_path: Path) -> None:
        # Two versions expected, one left nothing at all. The shard tally alone
        # cannot see this, which is what --expected-groups is for.
        self._version(tmp_path, "py311", 1, _suite(_passing("a")))

        results = btr.collect_groups(
            btr.find_shard_dirs(tmp_path),
            self.GROUP_RE,
            expected_shards=1,
            expected_groups=2,
        )

        assert (results.groups, results.missing_groups) == (1, 1)
        assert results.failed == 0
        assert results.conclusion == "failure"

    def test_a_short_version_fails_even_when_another_ran_extra(
        self, tmp_path: Path
    ) -> None:
        # py311 is one shard short; py312 ran an extra. The totals balance, so
        # only the per-group shortfall reveals it.
        self._version(tmp_path, "py311", 1, _suite(_passing("a")))
        for shard in (1, 2, 3):
            self._version(tmp_path, "py312", shard, _suite(_passing(f"b{shard}")))

        results = btr.collect_groups(
            btr.find_shard_dirs(tmp_path),
            self.GROUP_RE,
            expected_shards=2,
            expected_groups=2,
        )

        assert results.shards == 4
        assert results.group_shortfalls == 1
        assert results.conclusion == "failure"

    def test_an_unclassifiable_directory_fails_the_row(self, tmp_path: Path) -> None:
        # Artifact naming drifted away from the grouping pattern, so these
        # results belong to no version and are attributed to nothing.
        self._version(tmp_path, "py311", 1, _suite(_passing("a")))
        _write_shard(tmp_path, "some-other-artifact-name", _suite(_passing("b")))

        results = btr.collect_groups(
            btr.find_shard_dirs(tmp_path), self.GROUP_RE, expected_shards=1
        )

        assert results.unmatched_dirs == 1
        assert results.conclusion == "failure"

    def test_single_group_matches_ungrouped_counts(self, tmp_path: Path) -> None:
        # Grouping must be a no-op when only one version is present, so the
        # collapse to two rows cannot change what a narrowed run reports.
        for shard in (1, 2):
            self._version(
                tmp_path, "py313", shard, _suite(_passing(f"a{shard}"))
            )
        shard_dirs = btr.find_shard_dirs(tmp_path)

        grouped = btr.collect_groups(shard_dirs, self.GROUP_RE, expected_shards=2)
        flat = btr.collect_cell(shard_dirs, expected_shards=2)

        assert grouped.as_test_results() == flat.as_test_results()
        assert grouped.conclusion == flat.conclusion == "success"


@pytest.mark.parametrize(
    ("shards", "failed", "crashes", "unparsable", "expected"),
    [
        (0, 0, 0, 0, "failure"),
        (1, 0, 0, 0, "success"),
        (1, 1, 0, 0, "failure"),
        (1, 0, 1, 0, "failure"),
        (1, 0, 0, 1, "failure"),
        (5, 0, 0, 0, "success"),
    ],
)
def test_conclusion_truth_table(
    shards: int, failed: int, crashes: int, unparsable: int, expected: str
) -> None:
    results = btr.CellResults(
        shards=shards,
        failed=failed,
        crash_signals=crashes,
        unparsable_reports=unparsable,
    )
    assert results.conclusion == expected


@pytest.mark.parametrize(
    ("shards", "expected_shards", "missing", "conclusion"),
    [
        (5, 5, 0, "success"),
        (4, 5, 1, "failure"),
        (1, 5, 4, "failure"),
        (0, 5, 5, "failure"),
        (6, 5, 0, "success"),
        (1, 0, 0, "success"),
    ],
)
def test_shard_shortfall_truth_table(
    shards: int, expected_shards: int, missing: int, conclusion: str
) -> None:
    results = btr.CellResults(shards=shards, expected_shards=expected_shards)
    assert results.missing_shards == missing
    assert results.conclusion == conclusion
