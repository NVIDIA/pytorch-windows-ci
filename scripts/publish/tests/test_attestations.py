"""attestations.py: only files the hosted signing job of this run attested are publishable."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import attestations  # noqa: E402

REPO, RUN = "NVIDIA/pytorch-windows-ci", "4242"
SERVER = "https://github.com"
WHEEL = "torch-2.14.0.dev20261003+cu134-cp313-cp313-win_arm64.whl"
ABI3 = "torchaudio-2.11.0.dev20261003+cu134-cp310-abi3-win_arm64.whl"


def certificate(*, run: str = RUN, workflow: str = attestations.SIGNER_WORKFLOW,
                runner: str = "github-hosted") -> dict:
    return {"verificationResult": {"signature": {"certificate": {
        "runInvocationURI": f"{SERVER}/{REPO}/actions/runs/{run}/attempts/1",
        "buildSignerURI": f"{SERVER}/{REPO}/{workflow}@refs/heads/main",
        "runnerEnvironment": runner,
    }}}}


def gh(*results: dict, returncode: int = 0, stderr: str = ""):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, json.dumps(list(results)), stderr)

    run.calls = calls
    return run


def check(run, name: str = WHEEL) -> str | None:
    return attestations.check(Path(name), repository=REPO, run_id=RUN, server=SERVER, run=run)


def test_a_file_the_signing_job_attested_in_this_run_passes() -> None:
    run = gh(certificate())
    assert check(run) is None
    args = run.calls[0]
    assert args[:4] == ["gh", "attestation", "verify", WHEEL]
    assert args[args.index("--signer-workflow") + 1] == f"{REPO}/.github/workflows/_woa-sign.yml"
    assert "--deny-self-hosted-runners" in args


def test_an_earlier_runs_attestation_is_a_replay() -> None:
    problem = check(gh(certificate(run="1")))
    assert problem and "run 4242" in problem


def test_a_run_id_prefix_is_not_this_run() -> None:
    assert check(gh(certificate(run=RUN + "0")))


def test_a_self_hosted_attestation_is_refused() -> None:
    assert check(gh(certificate(runner="self-hosted")))


def test_another_workflows_attestation_is_refused() -> None:
    assert check(gh(certificate(workflow=".github/workflows/_woa-verify.yml")))


def test_one_matching_attestation_among_several_is_enough() -> None:
    assert check(gh(certificate(run="1"), certificate())) is None


def test_no_attestation_at_all_is_refused_with_the_reason() -> None:
    problem = check(gh(returncode=1, stderr="Error: no attestations found for subject"))
    assert problem and "no attestations found" in problem


def test_garbage_output_is_refused() -> None:
    run = lambda args, **kw: subprocess.CompletedProcess(args, 0, "not json", "")  # noqa: E731
    assert check(run)


def test_subjects_are_the_signing_jobs_files_and_nothing_else(tmp_path: Path) -> None:
    signed = tmp_path / "woa-py313-cu134-signed-4242"
    signed.mkdir()
    for name in (WHEEL, ABI3, "release-manifest-py313.json", "signature-report-py313.json"):
        (signed / name).write_text("x")
    (tmp_path / "validation-py313.json").write_text("x")
    assert [p.name for p in attestations.subjects(tmp_path)] == sorted(
        [WHEEL, ABI3, "release-manifest-py313.json", "signature-report-py313.json"]
    )


def test_a_set_without_wheels_is_refused(tmp_path: Path) -> None:
    (tmp_path / "release-manifest-py313.json").write_text("x")
    with pytest.raises(ValueError):
        attestations.subjects(tmp_path)


def test_main_checks_every_file_and_fails_on_any_problem(tmp_path: Path, capsys) -> None:
    (tmp_path / WHEEL).write_text("x")
    (tmp_path / "signature-report-py313.json").write_text("x")
    # Sorted order: the signature report, then the wheel.
    replies = iter([certificate(), certificate(run="1")])

    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, json.dumps([next(replies)]), "")

    argv = ["--asset-dir", str(tmp_path), "--repository", REPO, "--run-id", RUN, "--server-url", SERVER]
    assert attestations.main(argv, run=run) == 1
    err = capsys.readouterr().err
    assert f"::error title=attestations::{WHEEL}" in err
    assert "signature-report" not in err


def test_main_passes_a_fully_attested_set(tmp_path: Path) -> None:
    (tmp_path / WHEEL).write_text("x")
    argv = ["--asset-dir", str(tmp_path), "--repository", REPO, "--run-id", RUN, "--server-url", SERVER]
    assert attestations.main(argv, run=gh(certificate())) == 0
