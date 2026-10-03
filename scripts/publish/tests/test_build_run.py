# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``build_run.py``.

This decides whose wheels get our signature. The cases that matter most are the
builds that must never be signable - a relayed PR, a fork, some other workflow -
and the requests that would quietly publish less than was asked for. See Note
[Only our own scheduled or dispatched builds are signable].
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import build_run as br  # noqa: E402

REPO = "NVIDIA/pytorch-windows-ci"
RUN = 900


def run(**overrides) -> dict:
    data = {
        "id": RUN,
        "path": br.BUILD_WORKFLOW,
        "event": "schedule",
        "head_branch": "main",
        "head_sha": "a" * 40,
        "html_url": f"https://github.com/{REPO}/actions/runs/{RUN}",
        "head_repository": {"full_name": REPO},
    }
    data.update(overrides)
    return data


def jobs(*labels: str, failed: tuple[str, ...] = ()) -> list[dict]:
    out = [{"name": "resolve pytorch ref", "conclusion": "success"}]
    out += [{"name": f"woa-{label}-cu134-build / build", "conclusion": "success"} for label in labels]
    out += [{"name": f"woa-{label}-cu134-build / build", "conclusion": "failure"} for label in failed]
    out += [{"name": f"woa-{label}-cu134-arm64-test / test (shard 1/4)", "conclusion": "failure"} for label in labels]
    return out


def artifacts(*labels: str, expired: tuple[str, ...] = (), run_id: int = RUN) -> list[dict]:
    out = [{"name": f"woa-{label}-cu134-{run_id}", "expired": False} for label in labels]
    out += [{"name": f"woa-{label}-cu134-{run_id}", "expired": True} for label in expired]
    out.append({"name": f"build-logs-woa-py313-{run_id}-1", "expired": False})
    return out


def resolve(run_data=None, job_data=None, artifact_data=None, **kwargs):
    kwargs.setdefault("repository", REPO)
    kwargs.setdefault("channel", "nightly")
    kwargs.setdefault("default_branch", "main")
    return br.resolve(run_data or run(), job_data if job_data is not None else jobs("py313"),
                      artifact_data if artifact_data is not None else artifacts("py313"), **kwargs)


@pytest.mark.parametrize("label, version", [("py311", "3.11"), ("py313", "3.13"), ("py314t", "3.14t")])
def test_label_to_version(label: str, version: str) -> None:
    assert br.label_to_version(label) == version


def test_every_cell_that_built_is_eligible_in_version_order() -> None:
    labels = ("py314t", "py311", "py314", "py313", "py312")
    result = resolve(job_data=jobs(*labels), artifact_data=artifacts(*labels))
    assert [c["label"] for c in result["cells"]] == ["py311", "py312", "py313", "py314", "py314t"]
    assert result["cells"][-1] == {"version": "3.14t", "label": "py314t"}
    assert (result["run_id"], result["head_sha"], result["event"]) == (str(RUN), "a" * 40, "schedule")


def test_a_failed_test_shard_does_not_block_publication() -> None:
    """The run's own conclusion is never consulted - see Note [Per-cell build success, not run success]."""
    result = resolve(run(conclusion="failure"))
    assert [c["label"] for c in result["cells"]] == ["py313"]


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"event": "repository_dispatch"}, "only"),
        ({"event": "pull_request"}, "only"),
        ({"head_repository": {"full_name": "someone/fork"}}, "built 'someone/fork'"),
        ({"path": ".github/workflows/windows-rtx-build-test.yml"}, "not .github/workflows/windows-woa-build-test.yml"),
    ],
)
def test_untrusted_builds_are_never_signable(overrides: dict, message: str) -> None:
    with pytest.raises(br.BuildRunError, match=message):
        resolve(run(**overrides))


def test_a_dispatched_build_is_signable() -> None:
    assert resolve(run(event="workflow_dispatch"))["event"] == "workflow_dispatch"


@pytest.mark.parametrize("channel", ["rehearsal", "nightly", "release"])
def test_only_default_branch_builds_are_signable(channel: str) -> None:
    with pytest.raises(br.BuildRunError, match="only main builds are signable"):
        resolve(run(head_branch="feature/x"), channel=channel)
    assert resolve(run(), channel=channel)["head_branch"] == "main"


def test_cells_without_a_build_or_an_artifact_are_left_out() -> None:
    result = resolve(
        job_data=jobs("py313", "py312", failed=("py311",)),
        artifact_data=artifacts("py313", "py311", expired=("py312",)) + artifacts("py314", run_id=899),
    )
    assert [c["label"] for c in result["cells"]] == ["py313"]


def test_python_versions_selects_a_subset() -> None:
    result = resolve(job_data=jobs("py313", "py312"), artifact_data=artifacts("py313", "py312"), python_versions="3.13")
    assert result["cells"] == [{"version": "3.13", "label": "py313"}]


@pytest.mark.parametrize(
    "requested, message",
    [
        ("3.13,3.11", "3.11: its build job did not succeed"),
        ("3.12", "3.12: its wheel artifact is missing or expired"),
    ],
)
def test_requesting_an_unpublishable_cell_is_an_error(requested: str, message: str) -> None:
    with pytest.raises(br.BuildRunError, match=message):
        resolve(job_data=jobs("py313", "py312", failed=("py311",)),
                artifact_data=artifacts("py313", expired=("py312",)), python_versions=requested)


def test_a_run_with_nothing_publishable_is_an_error() -> None:
    with pytest.raises(br.BuildRunError, match="no cell"):
        resolve(job_data=jobs(failed=("py313",)), artifact_data=artifacts("py313"))


def test_github_api_pages_through_jobs() -> None:
    pages = {1: [{"name": f"j{i}"} for i in range(100)], 2: [{"name": "last"}]}

    def runner(command, **_):
        page = int(command[2].rsplit("page=", 1)[1])
        return subprocess.CompletedProcess(command, 0, json.dumps({"jobs": pages[page]}), "")

    assert len(br.GitHubApi(REPO, runner=runner).jobs("900")) == 101


def test_main_writes_the_matrix_for_the_workflow(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(br.GitHubApi, "run", lambda self, _id: run())
    monkeypatch.setattr(br.GitHubApi, "jobs", lambda self, _id: jobs("py313", "py314t"))
    monkeypatch.setattr(br.GitHubApi, "artifacts", lambda self, _id: artifacts("py313", "py314t"))
    output = tmp_path / "out"
    assert br.main(["--run-id", str(RUN), "--repository", REPO, "--channel", "nightly",
                    "--default-branch", "main", "--github-output", str(output)]) == 0
    lines = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(lines["cells"]) == [{"version": "3.13", "label": "py313"}, {"version": "3.14t", "label": "py314t"}]
    assert lines["run-id"] == str(RUN)


def test_main_rejects_a_non_numeric_run_id(capsys) -> None:
    assert br.main(["--run-id", "12;rm", "--repository", REPO, "--channel", "nightly", "--default-branch", "main"]) == 1
    assert "::error title=build run::" in capsys.readouterr().err
