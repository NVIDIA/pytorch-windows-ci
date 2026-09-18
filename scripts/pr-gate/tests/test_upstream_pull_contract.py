# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract tests between `upstream-pull.yml` and what it claims to start.

The audit record names the pipelines from the workflow-level `PIPELINES` list,
but the pipelines themselves are separate `uses:` jobs - a reusable-workflow
call cannot be generated from a list. So the same set is written twice, a few
dozen lines apart, and nothing in GitHub Actions checks that the two agree.

The failure is quiet and in the worst direction: adding a pipeline without
updating `PIPELINES` leaves an audit line that under-reports what an approval
authorised. These tests read the workflow and assert the couplings hold, so the
mismatch surfaces in lint rather than in a record nobody re-reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"
UPSTREAM_PULL = WORKFLOWS / "upstream-pull.yml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def workflow() -> dict:
    return load(UPSTREAM_PULL)


def reusable_jobs(workflow: dict) -> dict[str, str]:
    """Jobs that call a local reusable workflow, mapped to its filename."""
    return {
        name: job["uses"].rsplit("/", 1)[-1]
        for name, job in workflow["jobs"].items()
        if "uses" in job
    }


def test_pipelines_matches_the_jobs_that_call_them(workflow: dict) -> None:
    """`PIPELINES` is what the audit record reports, so it must be the truth."""
    declared = set(str(workflow["env"]["PIPELINES"]).split())
    called = set(reusable_jobs(workflow).values())

    assert declared == called, (
        "PIPELINES and the reusable-workflow jobs have drifted.\n"
        f"  PIPELINES: {sorted(declared)}\n"
        f"  called   : {sorted(called)}\n"
        "Adding or removing a pipeline must update both, in one commit."
    )


def test_every_named_pipeline_exists(workflow: dict) -> None:
    """A typo in `PIPELINES` would otherwise only show up in an audit line."""
    for name in str(workflow["env"]["PIPELINES"]).split():
        assert (WORKFLOWS / name).is_file(), f"{name} does not exist"


def test_pipelines_are_pinned_to_the_approved_sha(workflow: dict) -> None:
    """The point of the gate is that what runs is what was approved.

    Passing the PR number instead would let each pipeline re-resolve the head,
    which can move between the approval and the build.
    """
    for name in reusable_jobs(workflow):
        with_ = workflow["jobs"][name].get("with") or {}
        assert with_.get("pytorch-ref") == "${{ needs.gate.outputs.head-sha }}", (
            f"job {name!r} does not pin pytorch-ref to the gate's head SHA"
        )
        assert "pytorch-pr" not in with_, (
            f"job {name!r} passes pytorch-pr, which re-resolves the head"
        )


def test_pipelines_run_only_after_the_audit_record(workflow: dict) -> None:
    """No pipeline should be able to start without a record of who allowed it."""
    for name in reusable_jobs(workflow):
        needs = workflow["jobs"][name]["needs"]
        assert "authorize" in needs, f"job {name!r} does not need 'authorize'"


def test_called_pipelines_accept_workflow_call(workflow: dict) -> None:
    """A `uses:` target without `workflow_call` fails the whole run at startup."""
    for name in reusable_jobs(workflow).values():
        called = load(WORKFLOWS / name)
        # `on` parses as the boolean True in YAML 1.1.
        assert "workflow_call" in called[True], f"{name} has no workflow_call trigger"
        inputs = called[True]["workflow_call"].get("inputs") or {}
        assert "pytorch-ref" in inputs, f"{name} takes no pytorch-ref input"


def test_approval_is_environment_gated(workflow: dict) -> None:
    """The approval is enforced by the environment, not by anything in the file."""
    assert workflow["jobs"]["approval"]["environment"] == "pr-ci-approval"


def test_gate_and_inspect_carry_the_same_mode_guard(workflow: dict) -> None:
    """In the default `off` state a dispatch must cost nothing at all."""
    guard = workflow["jobs"]["gate"]["if"]
    assert "PR_CI_MODE" in guard
    assert workflow["jobs"]["inspect-dispatch"]["if"] == guard
