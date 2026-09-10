# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``resolve_cell_conclusion.py``."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import resolve_cell_conclusion as rcc  # noqa: E402

BUILD_CELL = "wheel-py312-cu130-build"
TEST_CELL = "wheel-py312-cu130-sm89-test"


def _job(name: str, conclusion: str | None = "success") -> dict:
    return {
        "name": name,
        "conclusion": conclusion,
        "html_url": f"https://github.com/o/r/actions/runs/1/job/{abs(hash(name))}",
    }


def _shards(cell: str, *conclusions: str) -> list[dict]:
    return [
        _job(f"{cell} / test (shard {i}/5)", c)
        for i, c in enumerate(conclusions, start=1)
    ]


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("success", "success"),
            ("neutral", "success"),
            ("skipped", "skipped"),
            ("cancelled", "cancelled"),
            ("failure", "failure"),
            ("timed_out", "failure"),
            ("startup_failure", "failure"),
            ("action_required", "failure"),
        ],
    )
    def test_maps_github_conclusions(self, raw: str, expected: str) -> None:
        assert rcc.normalize(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "something_new", 7])
    def test_unknown_conclusions_fail_closed(self, raw: object) -> None:
        assert rcc.normalize(raw) == "failure"


class TestSelectCellJobs:
    def test_matches_reusable_workflow_children(self) -> None:
        jobs = _shards(TEST_CELL, *["success"] * 5)
        assert len(rcc.select_cell_jobs(jobs, TEST_CELL)) == 5

    def test_matches_a_plain_job_of_the_same_name(self) -> None:
        assert len(rcc.select_cell_jobs([_job(BUILD_CELL)], BUILD_CELL)) == 1

    def test_ignores_other_cells(self) -> None:
        jobs = [
            *_shards(TEST_CELL, "success"),
            *_shards("wheel-py312-cu130-sm120-test", "failure"),
            _job("wheel-py312-cu132-build / build", "failure"),
            _job("test-summary", "success"),
        ]
        selected = rcc.select_cell_jobs(jobs, TEST_CELL)
        assert [j.conclusion for j in selected] == ["success"]

    def test_separator_prevents_prefix_bleed(self) -> None:
        # `...-build` must not swallow a hypothetical `...-build-extra` cell.
        jobs = [_job(f"{BUILD_CELL}-extra / build", "failure")]
        assert rcc.select_cell_jobs(jobs, BUILD_CELL) == []

    def test_skips_entries_without_a_name(self) -> None:
        assert rcc.select_cell_jobs([{"conclusion": "success"}], BUILD_CELL) == []


class TestResolveCell:
    def test_all_green_is_success(self) -> None:
        jobs = _shards(TEST_CELL, *["success"] * 5)
        assert rcc.resolve_cell(jobs, TEST_CELL).conclusion == "success"

    def test_one_failed_shard_fails_the_cell(self) -> None:
        jobs = _shards(TEST_CELL, "success", "success", "failure", "success", "success")
        assert rcc.resolve_cell(jobs, TEST_CELL).conclusion == "failure"

    def test_failure_outranks_cancellation(self) -> None:
        jobs = _shards(TEST_CELL, "failure", "cancelled", "cancelled")
        assert rcc.resolve_cell(jobs, TEST_CELL).conclusion == "failure"

    def test_cancellation_outranks_success(self) -> None:
        jobs = _shards(TEST_CELL, "success", "cancelled")
        assert rcc.resolve_cell(jobs, TEST_CELL).conclusion == "cancelled"

    def test_timed_out_shard_fails_the_cell(self) -> None:
        jobs = _shards(TEST_CELL, "success", "timed_out")
        assert rcc.resolve_cell(jobs, TEST_CELL).conclusion == "failure"

    def test_shard_still_running_fails_closed(self) -> None:
        jobs = _shards(TEST_CELL, "success", "success")
        jobs.append(_job(f"{TEST_CELL} / test (shard 3/5)", None))
        assert rcc.resolve_cell(jobs, TEST_CELL).conclusion == "failure"

    def test_filtered_cell_is_skipped_and_not_reported(self) -> None:
        jobs = _shards(TEST_CELL, *["skipped"] * 5)
        result = rcc.resolve_cell(jobs, TEST_CELL)
        assert result.conclusion == "skipped"
        assert result.should_report is False

    def test_a_real_result_beats_sibling_skips(self) -> None:
        jobs = _shards(TEST_CELL, "skipped", "success")
        result = rcc.resolve_cell(jobs, TEST_CELL)
        assert result.conclusion == "success"
        assert result.should_report is True

    def test_missing_cell_raises_rather_than_guessing(self) -> None:
        with pytest.raises(LookupError, match="no job matched cell"):
            rcc.resolve_cell([_job("something-else / build")], BUILD_CELL)

    def test_missing_cell_error_lists_available_jobs(self) -> None:
        with pytest.raises(LookupError, match="something-else / build"):
            rcc.resolve_cell([_job("something-else / build")], BUILD_CELL)


class TestResolveCells:
    """Reducing several cells into the one conclusion a combined row reports.

    WoA files a single build row and a single test row covering all five Python
    versions, so the row has to carry the worst outcome across them.
    """

    CELLS = [f"woa-{label}-cu134-build" for label in ("py311", "py312", "py313")]

    def _cells(self, *conclusions: str) -> list[dict]:
        return [
            _job(f"{cell} / build", conclusion)
            for cell, conclusion in zip(self.CELLS, conclusions)
        ]

    def test_all_green_is_success(self) -> None:
        result = rcc.resolve_cells(self._cells("success", "success", "success"), self.CELLS)
        assert result.conclusion == "success"
        assert result.should_report is True

    def test_one_failed_version_fails_the_row(self) -> None:
        # The whole point of the reduction: py313 failing to build is not a
        # green build row just because the other two succeeded.
        result = rcc.resolve_cells(self._cells("success", "success", "failure"), self.CELLS)
        assert result.conclusion == "failure"

    def test_narrowed_run_reports_the_versions_that_ran(self) -> None:
        # Two versions filtered out of the run; the one that ran decides.
        result = rcc.resolve_cells(self._cells("skipped", "skipped", "success"), self.CELLS)
        assert result.conclusion == "success"
        assert result.should_report is True

    def test_wholly_narrowed_run_reports_nothing(self) -> None:
        result = rcc.resolve_cells(self._cells("skipped", "skipped", "skipped"), self.CELLS)
        assert result.conclusion == "skipped"
        assert result.should_report is False

    def test_failure_outranks_a_cancelled_sibling(self) -> None:
        result = rcc.resolve_cells(self._cells("cancelled", "success", "failure"), self.CELLS)
        assert result.conclusion == "failure"

    def test_jobs_from_every_cell_are_collected(self) -> None:
        result = rcc.resolve_cells(self._cells("success", "success", "success"), self.CELLS)
        assert len(result.jobs) == 3

    def test_a_drifted_cell_name_still_raises(self) -> None:
        # One bad name must not be silently dropped from the reduction, or the
        # row would quietly stop covering that version.
        jobs = self._cells("success", "success", "success")
        with pytest.raises(LookupError, match="no job matched cell"):
            rcc.resolve_cells(jobs, [*self.CELLS, "woa-py999-cu134-build"])

    def test_single_cell_behaves_like_resolve_cell(self) -> None:
        jobs = _shards(TEST_CELL, "success", "failure")
        assert (
            rcc.resolve_cells(jobs, [TEST_CELL]).conclusion
            == rcc.resolve_cell(jobs, TEST_CELL).conclusion
        )

    def test_no_cells_raises(self) -> None:
        with pytest.raises(LookupError):
            rcc.resolve_cells(self._cells("success"), [])


class TestLoadJobs:
    def test_accepts_the_api_wrapper_object(self, tmp_path: Path) -> None:
        path = tmp_path / "jobs.json"
        path.write_text(json.dumps({"jobs": _shards(TEST_CELL, "success")}), "utf-8")
        assert len(rcc.load_jobs(path)) == 1

    def test_accepts_json_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "jobs.jsonl"
        path.write_text(
            "\n".join(json.dumps(j) for j in _shards(TEST_CELL, "success", "failure")),
            encoding="utf-8",
        )
        assert len(rcc.load_jobs(path)) == 2

    def test_empty_file_yields_no_jobs(self, tmp_path: Path) -> None:
        path = tmp_path / "jobs.json"
        path.write_text("", encoding="utf-8")
        assert rcc.load_jobs(path) == []


class TestMain:
    def _run(self, tmp_path: Path, jobs: list[dict], cell: str) -> tuple[int, dict]:
        jobs_file = tmp_path / "jobs.json"
        jobs_file.write_text(json.dumps(jobs), encoding="utf-8")
        output = tmp_path / "out.env"
        code = rcc.main(
            [
                "--jobs-json",
                str(jobs_file),
                "--cell",
                cell,
                "--github-output",
                str(output),
            ]
        )
        values = (
            dict(
                line.split("=", 1)
                for line in output.read_text(encoding="utf-8").splitlines()
            )
            if output.exists()
            else {}
        )
        return code, values

    def test_emits_conclusion_and_report_flag(self, tmp_path: Path) -> None:
        code, values = self._run(
            tmp_path, _shards(TEST_CELL, "success", "failure"), TEST_CELL
        )
        assert code == 0
        assert values["conclusion"] == "failure"
        assert values["should-report"] == "true"
        assert values["matched-jobs"] == "2"

    def test_skipped_cell_is_not_reported(self, tmp_path: Path) -> None:
        code, values = self._run(tmp_path, _shards(TEST_CELL, "skipped"), TEST_CELL)
        assert code == 0
        assert values["should-report"] == "false"

    def test_exits_non_zero_on_name_drift(self, tmp_path: Path) -> None:
        code, _ = self._run(tmp_path, [_job("unrelated")], BUILD_CELL)
        assert code == 1

    def test_summary_lists_every_contributing_job(self, tmp_path: Path) -> None:
        jobs_file = tmp_path / "jobs.json"
        jobs_file.write_text(
            json.dumps(_shards(TEST_CELL, "success", "failure")), encoding="utf-8"
        )
        summary = tmp_path / "summary.md"

        rcc.main(
            [
                "--jobs-json",
                str(jobs_file),
                "--cell",
                TEST_CELL,
                "--summary",
                str(summary),
            ]
        )

        text = summary.read_text(encoding="utf-8")
        assert "shard 1/5" in text and "shard 2/5" in text
        assert "`failure`" in text
