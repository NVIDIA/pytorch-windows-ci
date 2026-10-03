# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``wheel_repack.py``.

The repack is the last thing that touches the wheel bytes before they are
published, so the cases that matter are the ones where signing went subtly
wrong: a native file skipped, a file added, the input swapped underneath us.
Each has to stop the set rather than produce a plausible-looking wheel - see
Note [Every native file must come back changed].
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wheel_repack as wr  # noqa: E402

VERSION = "2.14.0.dev20260928+cu134"
DATE = (2026, 9, 28, 1, 2, 4)


def make_wheel(directory: Path, package: str, *, python: str = "cp313", abi: str | None = None,
               platform: str = "win_arm64", extra: dict[str, bytes] | None = None, native: bool = True) -> Path:
    name = f"{package}-{VERSION}-{python}-{abi or python}-{platform}.whl"
    dist_info = f"{package}-{VERSION}.dist-info"
    files: dict[str, bytes] = {f"{package}/__init__.py": b"# init\n"}
    if native:
        files[f"{package}/lib/{package}_core.dll"] = f"{package} dll".encode()
        files[f"{package}/_C.pyd"] = f"{package} pyd".encode()
    files.update(extra or {})
    files[f"{dist_info}/METADATA"] = b"Metadata-Version: 2.1\n"
    files[f"{dist_info}/RECORD"] = b"stale,,\n"
    path = directory / name
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(zipfile.ZipInfo(f"{package}/", date_time=DATE), b"")
        for member, data in files.items():
            info = zipfile.ZipInfo("placeholder", date_time=DATE)
            # Assigned after construction: ZipInfo() rewrites backslashes to "/" on
            # Windows, which would quietly defuse the unsafe-path cases below.
            info.filename = member
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return path


def make_set(directory: Path, **kwargs) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    return [make_wheel(directory, p, **kwargs) for p in wr.EXPECTED_PACKAGES]


def sign_tree(work: Path) -> None:
    """Stand in for Artifact Signing: every native file gains trailing bytes."""
    for path in (work / "unpacked").rglob("*"):
        if path.is_file() and wr.is_native(path.name):
            path.write_bytes(path.read_bytes() + b"<authenticode>")


def record_digest(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


# -- filename and set selection ---------------------------------------------


def test_parse_wheel_name_splits_tags_and_local_version() -> None:
    parsed = wr.parse_wheel_name(f"torch-{VERSION}-cp314t-cp314t-win_arm64.whl")
    assert (parsed.package, parsed.python, parsed.abi, parsed.platform) == ("torch", "cp314t", "cp314t", "win_arm64")
    assert parsed.public_version == "2.14.0.dev20260928"


def test_parse_wheel_name_rejects_a_non_wheel() -> None:
    with pytest.raises(ValueError):
        wr.parse_wheel_name("torch.tar.gz")


def test_select_wheels_returns_the_set_in_package_order(tmp_path: Path) -> None:
    make_set(tmp_path)
    assert [wr.parse_wheel_name(p.name).package for p in wr.select_wheels(tmp_path)] == list(wr.EXPECTED_PACKAGES)


def test_select_wheels_requires_every_package(tmp_path: Path) -> None:
    make_wheel(tmp_path, "torch")
    make_wheel(tmp_path, "torchvision")
    with pytest.raises(ValueError, match="torchaudio"):
        wr.select_wheels(tmp_path)


@pytest.mark.parametrize(
    "setup, message",
    [
        (lambda d: make_wheel(d, "numpy"), "unexpected package"),
        (lambda d: make_wheel(d, "torch", python="cp312"), "exactly one torch"),
    ],
)
def test_select_wheels_refuses_extras(tmp_path: Path, setup, message: str) -> None:
    make_set(tmp_path)
    setup(tmp_path)
    with pytest.raises(ValueError, match=message):
        wr.select_wheels(tmp_path)


def test_select_wheels_refuses_other_platforms(tmp_path: Path) -> None:
    make_wheel(tmp_path, "torch", platform="win_amd64")
    with pytest.raises(ValueError, match="win_arm64"):
        wr.select_wheels(tmp_path)


def test_select_wheels_refuses_a_mixed_cell(tmp_path: Path) -> None:
    make_wheel(tmp_path, "torch")
    make_wheel(tmp_path, "torchaudio")
    make_wheel(tmp_path, "torchvision", python="cp312")
    with pytest.raises(ValueError, match="one cell"):
        wr.select_wheels(tmp_path)


@pytest.mark.parametrize("torch_python", ["cp310", "cp313", "cp314"])
def test_select_wheels_accepts_a_stable_abi_extension(tmp_path: Path, torch_python: str) -> None:
    make_wheel(tmp_path, "torch", python=torch_python)
    make_wheel(tmp_path, "torchaudio", python="cp310", abi="abi3")
    make_wheel(tmp_path, "torchvision", python=torch_python)
    assert [p.name.split("-")[3] for p in wr.select_wheels(tmp_path)] == [torch_python, "abi3", torch_python]


@pytest.mark.parametrize(
    "torch_python, torch_abi, audio_python",
    [
        ("cp313", "cp313", "cp314"),
        ("cp314", "cp314t", "cp310"),
    ],
    ids=["abi3 floor above the interpreter", "abi3 on a free-threaded interpreter"],
)
def test_select_wheels_refuses_a_stable_abi_wheel_the_cell_cannot_install(
    tmp_path: Path, torch_python: str, torch_abi: str, audio_python: str
) -> None:
    make_wheel(tmp_path, "torch", python=torch_python, abi=torch_abi)
    make_wheel(tmp_path, "torchaudio", python=audio_python, abi="abi3")
    make_wheel(tmp_path, "torchvision", python=torch_python, abi=torch_abi)
    with pytest.raises(ValueError, match="not installable"):
        wr.select_wheels(tmp_path)


# -- unpack ------------------------------------------------------------------


def test_unpack_records_every_native_file_with_its_hash(tmp_path: Path) -> None:
    make_set(tmp_path / "in")
    plan = wr.unpack(tmp_path / "in", tmp_path / "work")
    assert [w["package"] for w in plan["wheels"]] == list(wr.EXPECTED_PACKAGES)
    torch = plan["wheels"][0]
    assert sorted(n["path"] for n in torch["native"]) == ["torch/_C.pyd", "torch/lib/torch_core.dll"]
    for item in torch["native"]:
        on_disk = (tmp_path / "work" / torch["root"] / item["path"]).read_bytes()
        assert item["sha256"] == hashlib.sha256(on_disk).hexdigest()
    assert json.loads((tmp_path / "work" / wr.PLAN_NAME).read_text()) == plan


def test_unpack_refuses_a_dirty_work_directory(tmp_path: Path) -> None:
    make_set(tmp_path / "in")
    (tmp_path / "work" / "unpacked" / "leftover").mkdir(parents=True)
    with pytest.raises(ValueError, match="not empty"):
        wr.unpack(tmp_path / "in", tmp_path / "work")


# `..\escape.dll` is refused on both platforms by different checks: on Windows,
# zipfile itself rewrites the backslash to "/" when reading, so the ".." check
# fires; elsewhere the backslash survives and the backslash check fires.
@pytest.mark.parametrize("member", ["../escape.dll", "/abs.dll", "C:/drive.dll", "..\\escape.dll"])
def test_unpack_refuses_paths_that_escape(tmp_path: Path, member: str) -> None:
    make_set(tmp_path / "in")
    make_wheel(tmp_path / "in", "torch", extra={member: b"x"})  # replaces the clean torch wheel
    with pytest.raises(ValueError, match="unsafe path"):
        wr.unpack(tmp_path / "in", tmp_path / "work")


def test_unpack_refuses_a_signed_record(tmp_path: Path) -> None:
    make_set(tmp_path / "in")
    make_wheel(tmp_path / "in", "torch", extra={f"torch-{VERSION}.dist-info/RECORD.jws": b"sig"})
    with pytest.raises(ValueError, match="signed RECORD"):
        wr.unpack(tmp_path / "in", tmp_path / "work")


def test_unpack_refuses_a_wheel_with_nothing_to_sign(tmp_path: Path) -> None:
    (tmp_path / "in").mkdir()
    make_wheel(tmp_path / "in", "torch")
    make_wheel(tmp_path / "in", "torchaudio")
    make_wheel(tmp_path / "in", "torchvision", native=False)
    with pytest.raises(ValueError, match="no native files"):
        wr.unpack(tmp_path / "in", tmp_path / "work")


# -- repack ------------------------------------------------------------------


@pytest.fixture
def signed_work(tmp_path: Path) -> Path:
    make_set(tmp_path / "in")
    wr.unpack(tmp_path / "in", tmp_path / "work")
    sign_tree(tmp_path / "work")
    return tmp_path / "work"


def test_repack_rewrites_record_and_preserves_the_archive(signed_work: Path, tmp_path: Path) -> None:
    manifest = wr.repack(signed_work, tmp_path / "out", provenance={"cell": "py313"})
    source = next((tmp_path / "in").glob("torch-*.whl"))
    original = zipfile.ZipFile(source)
    repacked = zipfile.ZipFile(tmp_path / "out" / source.name)

    assert [i.filename for i in repacked.infolist()] == [i.filename for i in original.infolist()]
    for before, after in zip(original.infolist(), repacked.infolist()):
        assert (after.date_time, after.external_attr) == (before.date_time, before.external_attr)
    assert repacked.read("torch/lib/torch_core.dll").endswith(b"<authenticode>")
    assert repacked.read("torch/__init__.py") == original.read("torch/__init__.py")

    record_name = f"torch-{VERSION}.dist-info/RECORD"
    rows = list(csv.reader(io.StringIO(repacked.read(record_name).decode())))
    listed = {row[0]: row for row in rows}
    assert listed[record_name] == [record_name, "", ""]
    for info in repacked.infolist():
        if info.is_dir() or info.filename == record_name:
            continue
        data = repacked.read(info.filename)
        assert listed[info.filename] == [info.filename, record_digest(data), str(len(data))]

    torch = manifest["wheels"][0]
    assert torch["native_file_count"] == 2
    assert torch["unsigned_sha256"] != torch["signed_sha256"]
    assert torch["signed_sha256"] == hashlib.sha256((tmp_path / "out" / torch["filename"]).read_bytes()).hexdigest()
    assert all(n["unsigned_sha256"] != n["signed_sha256"] for n in torch["native"])
    assert manifest["provenance"] == {"cell": "py313"}


def test_repack_refuses_a_native_file_that_came_back_unsigned(signed_work: Path, tmp_path: Path) -> None:
    pyd = signed_work / "unpacked" / "torchvision" / "torchvision" / "_C.pyd"
    pyd.write_bytes(b"torchvision pyd")  # restore the unsigned bytes
    with pytest.raises(ValueError, match=r"1 native file\(s\) came back unsigned: torchvision/_C.pyd"):
        wr.repack(signed_work, tmp_path / "out", provenance={})


def test_repack_refuses_a_tree_that_gained_a_file(signed_work: Path, tmp_path: Path) -> None:
    (signed_work / "unpacked" / "torch" / "torch" / "stray.tmp").write_bytes(b"x")
    with pytest.raises(ValueError, match="changed shape.*stray.tmp"):
        wr.repack(signed_work, tmp_path / "out", provenance={})


def test_repack_refuses_an_input_swapped_after_unpack(signed_work: Path, tmp_path: Path) -> None:
    source = next((tmp_path / "in").glob("torchaudio-*.whl"))
    source.write_bytes(source.read_bytes() + b"tamper")
    with pytest.raises(ValueError, match="changed since unpack"):
        wr.repack(signed_work, tmp_path / "out", provenance={})


def test_repack_never_overwrites_an_output(signed_work: Path, tmp_path: Path) -> None:
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / next((tmp_path / "in").glob("torch-*.whl")).name).write_bytes(b"old")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        wr.repack(signed_work, tmp_path / "out", provenance={})


def test_cli_round_trip_writes_the_manifest(tmp_path: Path, monkeypatch) -> None:
    make_set(tmp_path / "in")
    (tmp_path / "in" / "built_pytorch_sha.txt").write_text("a" * 40 + "\n")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_SHA", "b" * 40)
    assert wr.main(["unpack", "--wheel-dir", str(tmp_path / "in"), "--work-dir", str(tmp_path / "work")]) == 0
    sign_tree(tmp_path / "work")
    manifest_path = tmp_path / "out" / "release-manifest-py313.json"
    assert wr.main([
        "repack", "--work-dir", str(tmp_path / "work"), "--out-dir", str(tmp_path / "out"),
        "--manifest", str(manifest_path), "--cell", "py313",
        "--pytorch-sha-file", str(tmp_path / "in" / "built_pytorch_sha.txt"), "--build-run-id", "900",
    ]) == 0
    provenance = json.loads(manifest_path.read_text())["provenance"]
    assert provenance["pytorch_sha"] == "a" * 40
    assert (provenance["run_id"], provenance["commit_sha"], provenance["cell"]) == ("123", "b" * 40, "py313")
    assert provenance["build_run_id"] == "900"


def test_cli_reports_errors_as_annotations(tmp_path: Path, capsys) -> None:
    (tmp_path / "in").mkdir()
    assert wr.main(["unpack", "--wheel-dir", str(tmp_path / "in"), "--work-dir", str(tmp_path / "work")]) == 1
    assert "::error title=wheel repack::" in capsys.readouterr().err
