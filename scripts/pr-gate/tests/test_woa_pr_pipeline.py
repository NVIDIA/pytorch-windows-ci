# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""`_woa-pr-build-test.yml` must stay the nightly's build and test, and only that.

The PR path cannot call `windows-woa-build-test.yml` - that would make it grant
the nightly publication job's `contents: write` - so it calls a copy of the
nightly's build and test jobs instead. Copies drift: a cell enabled in the
nightly but not here goes untested on PRs, and an input changed in one but not
the other validates PRs against a different build than the one that ships.
These tests hold the two in step, and keep the copy from growing anything a PR
must not reach.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
NIGHTLY = "windows-woa-build-test.yml"
PR_PIPELINE = "_woa-pr-build-test.yml"
# Inputs only a manually started nightly can set, where it has a manual trigger at
# all; on the PR path they take the value the nightly falls back to.
DISPATCH_ONLY = {
    "runner-base": ("${{ inputs.runner-base || 'woa-arm64' }}", "woa-arm64"),
    "python-versions": ("${{ inputs.python-versions }}", None),
}


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def nightly() -> dict:
    return load(NIGHTLY)


@pytest.fixture(scope="module")
def pr() -> dict:
    return load(PR_PIPELINE)


def as_on_the_pr_path(with_: dict) -> dict:
    out = dict(with_)
    for key, (expression, pr_value) in DISPATCH_ONLY.items():
        if out.get(key) == expression:
            if pr_value is None:
                del out[key]
            else:
                out[key] = pr_value
    return out


def test_the_pr_pipeline_has_no_jobs_beyond_build_and_test(pr: dict) -> None:
    """No HUD reporting and no publication: neither has anything to do for a PR."""
    assert set(pr["jobs"]) == {"prep", "build", "test", "test-summary"}


@pytest.mark.parametrize("job", ["build", "test"])
def test_the_matrix_is_the_nightlys(nightly: dict, pr: dict, job: str) -> None:
    assert pr["jobs"][job]["strategy"] == nightly["jobs"][job]["strategy"]


@pytest.mark.parametrize("job", ["prep", "build", "test", "test-summary"])
def test_job_names_and_ordering_are_the_nightlys(nightly: dict, pr: dict, job: str) -> None:
    """The names are what a reader matches between a PR run and a nightly."""
    for key in ("name", "needs", "if", "uses"):
        assert pr["jobs"][job].get(key) == nightly["jobs"][job].get(key), (job, key)


@pytest.mark.parametrize("job", ["build", "test"])
def test_build_and_test_get_the_nightlys_inputs(nightly: dict, pr: dict, job: str) -> None:
    assert pr["jobs"][job]["with"] == as_on_the_pr_path(nightly["jobs"][job]["with"])


def test_the_summary_is_the_nightlys(nightly: dict, pr: dict) -> None:
    assert pr["jobs"]["test-summary"]["steps"] == nightly["jobs"]["test-summary"]["steps"]


def test_prep_provides_what_build_and_test_read(nightly: dict, pr: dict) -> None:
    outputs = set(pr["jobs"]["prep"]["outputs"])
    assert outputs <= set(nightly["jobs"]["prep"]["outputs"])
    text = yaml.safe_dump({j: pr["jobs"][j]["with"] for j in ("build", "test")})
    read = {part.split()[0] for part in text.split("needs.prep.outputs.")[1:]}
    assert read <= outputs, read - outputs


def test_the_pr_pipeline_builds_only_the_sha_it_was_given(pr: dict) -> None:
    script = next(s["run"] for s in pr["jobs"]["prep"]["steps"] if s.get("id") == "ref")
    assert 'require_sha pytorch "${REF}" "${REF}"' in script
    assert "resolve_ref \"https://github.com/pytorch/pytorch\"" not in script
    assert pr["jobs"]["prep"]["steps"][-1]["env"]["REF"] == "${{ inputs.pytorch-ref }}"
