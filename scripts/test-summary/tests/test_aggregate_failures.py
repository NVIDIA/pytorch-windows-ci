# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``aggregate_failures.py``."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import aggregate_failures as af  # noqa: E402


def _job(name, conclusion="success", url="https://example/job"):
    return {"name": name, "conclusion": conclusion, "html_url": url}


def test_coerce_jobs_accepts_api_object():
    payload = {"total_count": 1, "jobs": [_job("a")]}
    assert af._coerce_jobs(payload) == [_job("a")]


def test_coerce_jobs_accepts_array():
    payload = [_job("a"), _job("b")]
    assert af._coerce_jobs(payload) == payload


def test_coerce_jobs_handles_garbage():
    assert af._coerce_jobs("nope") == []


def test_load_jobs_from_array_file(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text(json.dumps([_job("a")]), encoding="utf-8")
    assert af.load_jobs(path) == [_job("a")]


def test_load_jobs_from_jsonlines_file(tmp_path):
    path = tmp_path / "jobs.jsonl"
    path.write_text(
        json.dumps(_job("a")) + "\n" + json.dumps(_job("b")) + "\n",
        encoding="utf-8",
    )
    assert af.load_jobs(path) == [_job("a"), _job("b")]


def test_load_jobs_empty(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("", encoding="utf-8")
    assert af.load_jobs(path) == []


def test_select_jobs_filters_by_include_and_exclude():
    jobs = [
        _job("wheel-py312-cu130-sm89-test / test (shard 1/5)", "failure"),
        _job("wheel-py312-cu130-build / build", "success"),
        _job("test-summary", "success"),
        _job("inspect relay dispatch (parked)", "success"),
    ]
    include = re.compile("(?i)test")
    exclude = re.compile("(?i)test-summary|summary")

    selected = af.select_jobs(jobs, include, exclude)

    names = {j.name for j in selected}
    assert names == {"wheel-py312-cu130-sm89-test / test (shard 1/5)"}


def test_select_jobs_uses_status_when_conclusion_missing():
    jobs = [{"name": "x-test", "status": "in_progress", "html_url": "u"}]
    selected = af.select_jobs(jobs, re.compile("(?i)test"), None)
    assert selected[0].conclusion == "in_progress"


def test_render_markdown_no_jobs():
    out = af.render_markdown([], "Title")
    assert "No test jobs were found" in out


def test_render_markdown_all_passed():
    jobs = [af.Job("a-test", "success", "u")]
    out = af.render_markdown(jobs, "Title")
    assert "0 of 1 test job(s) failed" in out
    assert "All test jobs passed." in out


def test_render_markdown_lists_failures_with_links():
    jobs = [
        af.Job("b-test", "failure", "https://example/b"),
        af.Job("a-test", "success", "https://example/a"),
        af.Job("c-test", "timed_out", "https://example/c"),
    ]
    out = af.render_markdown(jobs, "Title")
    assert "2 of 3 test job(s) failed" in out
    assert "[logs](https://example/b)" in out
    assert "[logs](https://example/c)" in out
    # Failing rows are sorted by name: b-test should precede c-test.
    assert out.index("b-test") < out.index("c-test")


def test_main_end_to_end(tmp_path, capsys):
    path = tmp_path / "jobs.json"
    path.write_text(
        json.dumps(
            {
                "jobs": [
                    _job("x-test", "failure", "https://example/x"),
                    _job("x-build", "success"),
                ]
            }
        ),
        encoding="utf-8",
    )
    rc = af.main(["--jobs-json", str(path), "--title", "Summary"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "1 of 1 test job(s) failed" in out
    assert "https://example/x" in out
