# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""index_check.py: nothing from the index is installed unless attestations vouch for its bytes.

See Note [The index is checked against attestations, not against itself].
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import attestations  # noqa: E402
import index_check as ic  # noqa: E402

REPO, RUN, TAG = "NVIDIA/pytorch-windows-ci", "4242", "woa-nightly-20261005-r4242"
SERVER = "https://github.com"
INDEX = "https://pypi.nvidia.com/nvtorch_oot_nightly"
VERSION = "2.14.0.dev20261005+cu134"
WHEELS = {
    f"torch-{VERSION}-cp313-cp313-win_arm64.whl": b"torch bytes",
    f"torchvision-0.25.0.dev20261005+cu134-cp313-cp313-win_arm64.whl": b"vision bytes",
    "torchaudio-2.11.0.dev20261005+cu134-cp310-abi3-win_arm64.whl": b"audio bytes",
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def report(**overrides) -> dict:
    data = {
        "repository": REPO, "run_id": RUN, "channel": "nightly", "tag": TAG, "draft": False, "immutable": True,
        "assets": [{"name": n, "sha256": sha(b)} for n, b in WHEELS.items()]
                  + [{"name": "release-manifest-py313.json", "sha256": "e" * 64}],
    }
    data.update(overrides)
    return data


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class Index:
    """pypi.nvidia.com's simple pages and files, as `listed` (fragment) and `served` (bytes)."""

    def __init__(self, *, listed: dict[str, str] | None = None, served: dict[str, bytes] | None = None,
                 extra_links: int = 0):
        self.listed = {n: sha(b) for n, b in WHEELS.items()} | (listed or {})
        self.served = dict(WHEELS) | (served or {})
        self.extra_links = extra_links
        self.fetched: list[str] = []

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.fetched.append(url)
        if url.endswith("/"):
            package = url.rstrip("/").rsplit("/", 1)[-1]
            links = [f'<a href="{urllib.parse.quote(n)}#sha256={d}">{n}</a>'
                     for n, d in self.listed.items() if n.startswith(f"{package}-")]
            links += links[:1] * self.extra_links
            return Response("\n".join(links).encode())
        return Response(self.served[urllib.parse.unquote(url.rsplit("/", 1)[-1])])


def gh(*, attested: set[str] | None = None, in_release: set[str] | None = None):
    """`gh attestation verify` and `gh release verify-asset` over files named in the sets."""
    attested = set(WHEELS) if attested is None else attested
    in_release = set(WHEELS) if in_release is None else in_release
    calls = []

    def run(args, **_):
        calls.append(args)
        path = Path(args[3] if args[1] == "attestation" else args[4])
        if args[1] == "attestation":
            if path.name not in attested:
                return subprocess.CompletedProcess(args, 1, "", "no matching attestations found")
            cert = {"runInvocationURI": f"{SERVER}/{REPO}/actions/runs/{RUN}/attempts/1",
                    "buildSignerURI": f"{SERVER}/{REPO}/{attestations.SIGNER_WORKFLOW}@refs/heads/main",
                    "runnerEnvironment": "github-hosted"}
            return subprocess.CompletedProcess(args, 0, json.dumps(
                [{"verificationResult": {"signature": {"certificate": cert}}}]), "")
        if path.name in in_release:
            return subprocess.CompletedProcess(args, 0, "Verification succeeded!", "")
        return subprocess.CompletedProcess(args, 1, "", f"attestation for {TAG} does not contain subject sha256:x")

    run.calls = calls
    return run


def check(tmp_path: Path, data: dict | None = None, *, index: Index | None = None, run=None) -> dict[str, str]:
    return ic.check(data or report(), channel="nightly", repository=REPO, run_id=RUN, download_dir=tmp_path / "d",
                    server=SERVER, opener=index or Index(), run=run or gh())


def test_every_released_wheel_passes_and_only_wheels_are_checked(tmp_path: Path) -> None:
    run = gh()
    assert check(tmp_path, run=run) == {n: sha(b) for n, b in WHEELS.items()}
    checked = {Path(c[3] if c[1] == "attestation" else c[4]).name for c in run.calls}
    assert checked == set(WHEELS)
    assert ["gh", "release", "verify-asset", TAG] == next(c for c in run.calls if c[1] == "release")[:4]
    assert list((tmp_path / "d").iterdir()) == []


def test_the_bytes_are_downloaded_from_the_index_not_the_release(tmp_path: Path) -> None:
    index = Index()
    check(tmp_path, index=index)
    files = [u for u in index.fetched if not u.endswith("/")]
    assert len(files) == len(WHEELS) and all(u.startswith(f"{INDEX}/") for u in files)


@pytest.mark.parametrize(
    "index, problem",
    [
        (Index(served={f"torch-{VERSION}-cp313-cp313-win_arm64.whl": b"swapped"}), "the index served sha256"),
        (Index(listed={f"torch-{VERSION}-cp313-cp313-win_arm64.whl": "0" * 64}), "the index lists sha256 000"),
        (Index(extra_links=1), "the index lists it 2 times"),
    ],
)
def test_an_index_copy_that_is_not_the_release_is_refused(tmp_path: Path, index: Index, problem: str) -> None:
    with pytest.raises(ic.IndexCheckError, match=problem):
        check(tmp_path, index=index)


def test_a_wheel_missing_from_the_index_is_refused(tmp_path: Path) -> None:
    index = Index()
    del index.listed["torchaudio-2.11.0.dev20261005+cu134-cp310-abi3-win_arm64.whl"]
    with pytest.raises(ic.IndexCheckError, match="torchaudio.*the index lists it 0 times"):
        check(tmp_path, index=index)


def test_a_wheel_without_the_signing_jobs_attestation_is_refused(tmp_path: Path) -> None:
    name = f"torch-{VERSION}-cp313-cp313-win_arm64.whl"
    with pytest.raises(ic.IndexCheckError, match=re.escape(f"{name}: no verifiable attestation")):
        check(tmp_path, run=gh(attested=set(WHEELS) - {name}))


def test_a_wheel_the_release_attestation_does_not_list_is_refused(tmp_path: Path) -> None:
    name = f"torch-{VERSION}-cp313-cp313-win_arm64.whl"
    with pytest.raises(ic.IndexCheckError, match=re.escape(f"{name}: not an attested asset of release {TAG}")):
        check(tmp_path, run=gh(in_release=set(WHEELS) - {name}))


@pytest.mark.parametrize(
    "overrides, problem",
    [
        ({"repository": "someone/fork"}, "repository is 'someone/fork'"),
        ({"run_id": "1"}, "run_id is '1'"),
        ({"channel": "release"}, "channel is 'release'"),
        ({"draft": True}, "not a published immutable release"),
        ({"immutable": False}, "not a published immutable release"),
        ({"assets": [{"name": "release-manifest-py313.json", "sha256": "e" * 64}]}, "lists no wheels"),
    ],
)
def test_a_report_for_anything_but_this_runs_immutable_release_is_refused(tmp_path: Path, overrides, problem) -> None:
    with pytest.raises(ic.IndexCheckError, match=problem):
        check(tmp_path, report(**overrides))


def test_main_writes_the_verified_hashes_as_one_output_line(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ic, "check", lambda data, **_: {n: sha(b) for n, b in WHEELS.items()})
    (tmp_path / "report.json").write_text(json.dumps(report()), encoding="utf-8")
    output = tmp_path / "out"
    assert ic.main(["--release-report", str(tmp_path / "report.json"), "--channel", "nightly", "--repository", REPO,
                    "--run-id", RUN, "--download-dir", str(tmp_path / "d"), "--github-output", str(output)]) == 0
    name, _, value = output.read_text(encoding="utf-8").partition("=")
    assert name == "wheels" and json.loads(value) == {n: sha(b) for n, b in WHEELS.items()}


def test_main_writes_nothing_when_a_check_fails(tmp_path: Path, monkeypatch, capsys) -> None:
    def refuse(data, **_):
        raise ic.IndexCheckError("torch: the index served sha256 x")

    monkeypatch.setattr(ic, "check", refuse)
    (tmp_path / "report.json").write_text(json.dumps(report()), encoding="utf-8")
    output = tmp_path / "out"
    assert ic.main(["--release-report", str(tmp_path / "report.json"), "--channel", "nightly", "--repository", REPO,
                    "--run-id", RUN, "--download-dir", str(tmp_path / "d"), "--github-output", str(output)]) == 1
    assert "::error title=index check::torch: the index served" in capsys.readouterr().err
    assert not output.exists()
