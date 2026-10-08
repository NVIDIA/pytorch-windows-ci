# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``github_release.py``.

What reaches this release is what Kitmaker distributes, so the cases that
matter are the ways a wrong set could be attached, or a right set could land
wrong: a wheel that skipped validation, an asset GitHub renamed, a digest that
does not match, a release that is not immutable. Each must stop publication.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import github_release as gr  # noqa: E402

VERSION = "2.14.0.dev20260928+cu134"
DATE = datetime(2026, 9, 28, tzinfo=timezone.utc)
REPO = "NVIDIA/pytorch-windows-ci"


def wheel_name(package: str, cell: str = "py313") -> str:
    abi = "cp" + cell[2:]
    return f"{package}-{VERSION}-{abi.rstrip('t')}-{abi}-win_arm64.whl"


def abi3_name(package: str) -> str:
    return f"{package}-{VERSION}-cp310-abi3-win_arm64.whl"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def py3_none_name(package: str) -> str:
    return f"{package}-{VERSION}-py3-none-win_arm64.whl"


def write_cell(directory: Path, cell: str = "py313", *, validation: str = "passed", signatures: str = "passed",
               tamper: str | None = None, packages=gr.PACKAGES, build_run: str = "900", abi3: tuple = (),
               py3_none: tuple = (), names: dict[str, str] | None = None) -> None:
    """A cell's wheels plus the three evidence files that must agree on them."""
    directory.mkdir(parents=True, exist_ok=True)
    wheels = []
    for package in packages:
        name = (names or {}).get(package) or (
            abi3_name(package) if package in abi3 else py3_none_name(package) if package in py3_none
            else wheel_name(package, cell))
        data = f"{package} {cell} signed".encode()
        (directory / name).write_bytes(data)
        wheels.append({"filename": name, "package": package, "version": VERSION, "signed_sha256": sha(data)})
    listed = [{"filename": w["filename"], "sha256": w["signed_sha256"]} for w in wheels]
    if tamper:
        (directory / tamper).write_bytes(b"swapped after validation")
    (directory / f"release-manifest-{cell}.json").write_text(json.dumps(
        {"provenance": {"pytorch_sha": "a" * 40, "build_run_id": build_run}, "wheels": wheels}))
    (directory / f"signature-report-{cell}.json").write_text(json.dumps({"status": signatures, "wheels": listed}))
    (directory / f"validation-{cell}.json").write_text(json.dumps({"status": validation, "wheels": listed}))


def remote_release(assets: list[gr.Asset], *, tag: str, draft=False, prerelease=True, immutable=True, **overrides) -> dict:
    release = {
        "id": 7,
        "tag_name": tag,
        "html_url": f"https://github.com/{REPO}/releases/tag/{tag}",
        "draft": draft,
        "prerelease": prerelease,
        "immutable": immutable,
        "assets": [
            {
                "name": a.name,
                "state": "uploaded",
                "size": a.size,
                "digest": f"sha256:{a.sha256}",
                "browser_download_url": f"https://github.com/{REPO}/releases/download/{tag}/{a.name}",
            }
            for a in assets
        ],
    }
    release.update(overrides)
    return release


# -- tags --------------------------------------------------------------------


@pytest.mark.parametrize(
    "channel, version, expected",
    [
        ("nightly", VERSION, "woa-nightly-20260928-r42"),
        ("rehearsal", VERSION, "woa-rehearsal-20260928-r42"),
        ("release", "2.14.0+cu134", "woa-v2.14.0-r42"),
    ],
)
def test_release_tag(channel: str, version: str, expected: str) -> None:
    assert gr.release_tag(channel, "42", date=DATE, torch_version=version) == expected


@pytest.mark.parametrize("channel", ["nightly", "rehearsal"])
def test_a_rerun_on_a_later_day_computes_the_same_tag(channel: str) -> None:
    next_day = datetime(2026, 9, 29, 0, 30, tzinfo=timezone.utc)
    assert gr.release_tag(channel, "42", date=next_day, torch_version=VERSION) == f"woa-{channel}-20260928-r42"


def test_a_dateless_rehearsal_falls_back_to_the_run_date() -> None:
    assert gr.release_tag("rehearsal", "42", date=DATE, torch_version="2.14.0+cu134") == "woa-rehearsal-20260928-r42"


def test_a_release_refuses_a_dated_build() -> None:
    with pytest.raises(ValueError, match="dateless"):
        gr.release_tag("release", "42", date=DATE, torch_version=VERSION)


@pytest.mark.parametrize("channel, run_id", [("manual", "42"), ("nightly", "42a")])
def test_release_tag_rejects_bad_inputs(channel: str, run_id: str) -> None:
    with pytest.raises(ValueError):
        gr.release_tag(channel, run_id, date=DATE, torch_version=VERSION)


# -- assets and evidence -----------------------------------------------------


def test_collect_assets_refuses_unexpected_files(tmp_path: Path) -> None:
    write_cell(tmp_path)
    (tmp_path / "built_pytorch_sha.txt").write_text("x")
    with pytest.raises(ValueError, match="unexpected file"):
        gr.collect_assets(tmp_path)


def test_collect_assets_enforces_the_2gib_limit(tmp_path: Path, monkeypatch) -> None:
    write_cell(tmp_path)
    monkeypatch.setattr(gr, "ASSET_SIZE_LIMIT", 10)
    monkeypatch.setattr(gr, "ASSET_SIZE_WARN", 5)
    with pytest.raises(ValueError, match="under 2 GiB"):
        gr.collect_assets(tmp_path)


def test_collect_assets_refuses_duplicate_names(tmp_path: Path) -> None:
    write_cell(tmp_path / "a")
    write_cell(tmp_path / "b")
    with pytest.raises(ValueError, match="duplicate asset names"):
        gr.collect_assets(tmp_path)


def test_collect_assets_requires_wheels(tmp_path: Path) -> None:
    tmp_path.joinpath("validation-py313.json").write_text("{}")
    with pytest.raises(ValueError, match="no wheels"):
        gr.collect_assets(tmp_path)


def test_check_evidence_accepts_a_consistent_multi_cell_set(tmp_path: Path) -> None:
    write_cell(tmp_path, "py313")
    write_cell(tmp_path, "py312")
    evidence = gr.check_evidence(gr.collect_assets(tmp_path))
    assert evidence["cells"] == ["py312", "py313"]
    assert evidence["torch_version"] == VERSION


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"validation": "failed"}, "validation did not pass"),
        ({"signatures": "failed"}, "signature verification did not pass"),
        ({"tamper": wheel_name("torchvision")}, "does not match its signed SHA-256"),
        ({"packages": ("torch", "torchvision")}, "expected"),
    ],
)
def test_check_evidence_refuses_an_unvalidated_set(tmp_path: Path, kwargs: dict, message: str) -> None:
    write_cell(tmp_path, **kwargs)
    with pytest.raises(ValueError, match=message):
        gr.check_evidence(gr.collect_assets(tmp_path))


def test_check_evidence_refuses_a_missing_report(tmp_path: Path) -> None:
    write_cell(tmp_path)
    (tmp_path / "validation-py313.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        gr.check_evidence(gr.collect_assets(tmp_path))


def test_a_shared_stable_abi_wheel_is_published_once_from_the_lowest_python(tmp_path: Path) -> None:
    for cell in ("py313", "py312"):
        write_cell(tmp_path / cell, cell, abi3=("torchaudio",))
    evidence = gr.check_evidence(gr.collect_assets(tmp_path))
    wheels = [a for a in evidence["assets"] if a.name.endswith(".whl")]
    assert sorted(a.name for a in wheels) == sorted(
        [abi3_name("torchaudio")] + [wheel_name(p, c) for p in ("torch", "torchvision") for c in ("py312", "py313")]
    )
    audio = next(a for a in wheels if a.name == abi3_name("torchaudio"))
    assert audio.sha256 == sha(b"torchaudio py312 signed")
    assert evidence["shared"] == {abi3_name("torchaudio"): "py312"}


def test_py3_none_wheels_every_cell_builds_are_published_once_from_the_lowest_python(tmp_path: Path) -> None:
    """The tagging pytorch/audio#4234 and pytorch/vision#9643 switched to on 2026-10-07,
    shared by the free-threaded cell too."""
    cells = ("py314t", "py313", "py312")
    for cell in cells:
        write_cell(tmp_path / cell, cell, py3_none=("torchaudio", "torchvision"))
    evidence = gr.check_evidence(gr.collect_assets(tmp_path))
    wheels = sorted(a.name for a in evidence["assets"] if a.name.endswith(".whl"))
    assert wheels == sorted([py3_none_name("torchaudio"), py3_none_name("torchvision")]
                            + [wheel_name("torch", c) for c in cells])
    assert evidence["shared"] == {py3_none_name("torchaudio"): "py312", py3_none_name("torchvision"): "py312"}


@pytest.mark.parametrize(
    "cell, names, refused",
    [
        ("py312", {"torchvision": wheel_name("torchvision", "py313")}, wheel_name("torchvision", "py313")),
        ("py314t", {"torchaudio": abi3_name("torchaudio")}, abi3_name("torchaudio")),
    ],
    ids=["another Python's wheel", "abi3 on a free-threaded cell"],
)
def test_a_wheel_the_cells_torch_cannot_install_is_refused(tmp_path: Path, cell: str, names: dict, refused: str) -> None:
    write_cell(tmp_path / "py313", "py313")
    write_cell(tmp_path / cell, cell, names=names)
    with pytest.raises(ValueError, match=f"cell {cell}: {refused.replace('+', '[+]')} does not install on its torch's"):
        gr.check_evidence(gr.collect_assets(tmp_path))


def test_a_torch_not_built_for_one_interpreter_is_refused(tmp_path: Path) -> None:
    write_cell(tmp_path, "py313", names={"torch": py3_none_name("torch")})
    with pytest.raises(ValueError, match="cell py313: .* is not built for exactly one CPython interpreter"):
        gr.check_evidence(gr.collect_assets(tmp_path))


def test_each_cell_vouches_for_its_own_copy_of_a_shared_wheel(tmp_path: Path) -> None:
    write_cell(tmp_path / "py312", "py312", abi3=("torchaudio",))
    write_cell(tmp_path / "py313", "py313", abi3=("torchaudio",), tamper=abi3_name("torchaudio"))
    with pytest.raises(ValueError, match="cell py313: .*abi3.* does not match its signed SHA-256"):
        gr.check_evidence(gr.collect_assets(tmp_path))


def test_publish_uploads_one_copy_of_a_shared_wheel(tmp_path: Path) -> None:
    for cell in ("py313", "py312"):
        write_cell(tmp_path / cell, cell, abi3=("torchaudio",))
    github = FakeGitHub()
    report = run_publish(tmp_path, github)
    names = [Path(f).name for f in github.created["files"]]
    assert names.count(abi3_name("torchaudio")) == 1
    assert report["shared_from"] == {abi3_name("torchaudio"): "py312"}


def test_check_evidence_refuses_a_wheel_nobody_vouched_for(tmp_path: Path) -> None:
    write_cell(tmp_path)
    (tmp_path / wheel_name("torch", "py312")).write_bytes(b"stray")
    with pytest.raises(ValueError, match="wheels with no manifest"):
        gr.check_evidence(gr.collect_assets(tmp_path))


# -- release verification ----------------------------------------------------


@pytest.fixture
def assets(tmp_path: Path) -> list[gr.Asset]:
    write_cell(tmp_path)
    return gr.collect_assets(tmp_path)


def test_verify_release_accepts_an_exact_match(assets) -> None:
    tag = "woa-nightly-20260928-r42"
    assert gr.verify_release(remote_release(assets, tag=tag), assets, tag=tag, draft=False,
                             prerelease=True, require_immutable=True) == []


def test_verify_release_catches_a_renamed_local_version(assets) -> None:
    """GitHub rewriting `+cu134` must be caught, not published."""
    tag = "woa-nightly-20260928-r42"
    release = remote_release(assets, tag=tag)
    for item in release["assets"]:
        item["name"] = item["name"].replace("+", ".")
    problems = gr.verify_release(release, assets, tag=tag, draft=False, prerelease=True, require_immutable=True)
    assert any("renamed by GitHub" in p for p in problems)
    assert any("missing from the release" in p for p in problems)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r["assets"][0].update(digest="sha256:" + "0" * 64), "digest"),
        (lambda r: r["assets"][0].update(size=1), "size"),
        (lambda r: r["assets"][0].update(state="starter"), "state"),
        (lambda r: r.update(immutable=False), "not immutable"),
        (lambda r: r.update(prerelease=False), "prerelease"),
    ],
)
def test_verify_release_catches_each_drift(assets, mutate, message: str) -> None:
    tag = "woa-nightly-20260928-r42"
    release = remote_release(assets, tag=tag)
    mutate(release)
    problems = gr.verify_release(release, assets, tag=tag, draft=False, prerelease=True, require_immutable=True)
    assert any(message in p for p in problems), problems


def test_a_draft_is_not_required_to_be_immutable(assets) -> None:
    tag = "woa-rehearsal-20260928-r42"
    release = remote_release(assets, tag=tag, draft=True, immutable=False)
    assert gr.verify_release(release, assets, tag=tag, draft=True, prerelease=True, require_immutable=True) == []


# -- gh wrapper --------------------------------------------------------------


def test_find_release_falls_back_to_the_listing_for_drafts() -> None:
    calls = []

    def runner(command, **_):
        calls.append(command)
        if "releases/tags/" in command[2]:
            return subprocess.CompletedProcess(command, 1, "", "gh: Not Found (HTTP 404)")
        return subprocess.CompletedProcess(command, 0, json.dumps([{"tag_name": "other"}, {"tag_name": "t", "draft": True}]), "")

    found = gr.GitHub(REPO, runner=runner).find_release("t")
    assert found == {"tag_name": "t", "draft": True}
    assert calls[1][2].startswith(f"repos/{REPO}/releases?per_page=100")


def test_find_release_surfaces_real_errors() -> None:
    def runner(command, **_):
        return subprocess.CompletedProcess(command, 1, "", "HTTP 500")

    with pytest.raises(RuntimeError):
        gr.GitHub(REPO, runner=runner).find_release("t")


def test_create_passes_prerelease_and_draft_flags(tmp_path: Path) -> None:
    seen = {}

    def runner(command, **_):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "", "")

    gr.GitHub(REPO, runner=runner).create(tag="t", target="s", files=[tmp_path / "a.whl"], notes="n",
                                          draft=True, prerelease=True)
    command = seen["command"]
    assert command[:4] == ["gh", "release", "create", "t"]
    assert {"--draft", "--prerelease", "--latest=false"} <= set(command)
    assert command[command.index("--target") + 1] == "s"


# -- publish -----------------------------------------------------------------


class FakeGitHub:
    def __init__(self, existing: dict | None = None):
        self.release, self.created = existing, None

    def find_release(self, tag):
        return self.release

    def create(self, *, tag, target, files, notes, draft, prerelease):
        self.created = dict(tag=tag, target=target, files=files, draft=draft, prerelease=prerelease, notes=notes)
        local = [gr.Asset(Path(f).name, Path(f), Path(f).stat().st_size, gr._sha256(Path(f))) for f in files]
        self.release = remote_release(local, tag=tag, draft=draft, prerelease=prerelease)


def run_publish(tmp_path: Path, github, channel="nightly", justification="", build_run_id="900"):
    return gr.publish(asset_dir=tmp_path, channel=channel, repository=REPO, target_sha="c" * 40, run_id="42",
                      build_run_id=build_run_id, justification=justification,
                      env={"GITHUB_REPOSITORY": REPO, "GITHUB_RUN_ID": "42"},
                      github=github, date=DATE, sleep=lambda _: None)


def test_publish_creates_then_verifies(tmp_path: Path) -> None:
    write_cell(tmp_path)
    github = FakeGitHub()
    report = run_publish(tmp_path, github)
    assert github.created["tag"] == "woa-nightly-20260928-r42"
    assert github.created["prerelease"] and not github.created["draft"]
    assert Path(github.created["files"][0]).suffix == ".whl"
    assert report["immutable"] is True
    urls = {a["name"]: a["url"] for a in report["assets"]}
    assert urls[wheel_name("torch")].endswith(f"/woa-nightly-20260928-r42/{wheel_name('torch')}")
    assert "a" * 40 in github.created["notes"]
    assert f"{REPO}/actions/runs/900" in github.created["notes"]
    assert report["build_run_id"] == "900"


def test_publish_refuses_wheels_from_another_build(tmp_path: Path) -> None:
    write_cell(tmp_path, "py313")
    write_cell(tmp_path, "py312", build_run="899")
    github = FakeGitHub()
    with pytest.raises(ValueError, match=r"not built by run 900: \{'py312': '899'\}"):
        run_publish(tmp_path, github)
    assert github.created is None


def test_publish_verifies_an_existing_release_without_touching_it(tmp_path: Path) -> None:
    write_cell(tmp_path)
    assets = gr.collect_assets(tmp_path)
    github = FakeGitHub(remote_release(assets, tag="woa-nightly-20260928-r42"))
    run_publish(tmp_path, github)
    assert github.created is None


def test_publish_refuses_to_repair_a_mismatched_existing_release(tmp_path: Path) -> None:
    write_cell(tmp_path)
    assets = gr.collect_assets(tmp_path)
    existing = remote_release(assets, tag="woa-nightly-20260928-r42")
    existing["assets"][0]["digest"] = "sha256:" + "0" * 64
    github = FakeGitHub(existing)
    with pytest.raises(ValueError, match="failed verification"):
        run_publish(tmp_path, github)
    assert github.created is None


def test_a_rehearsal_is_a_draft(tmp_path: Path) -> None:
    write_cell(tmp_path)
    github = FakeGitHub()
    report = run_publish(tmp_path, github, channel="rehearsal")
    assert github.created["draft"] is True
    assert report["tag"] == "woa-rehearsal-20260928-r42"


def test_a_release_needs_a_justification(tmp_path: Path) -> None:
    write_cell(tmp_path)
    with pytest.raises(ValueError, match="justification"):
        run_publish(tmp_path, FakeGitHub(), channel="release", justification="  ")
