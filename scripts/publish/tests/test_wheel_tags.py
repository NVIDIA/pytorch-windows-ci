# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``wheel_tags.py``.

The point of the module is to agree with pip about which wheels an interpreter
installs, whatever tags upstream chooses next; see
Note [Compatibility is pip's, not a list of tag shapes]. So the tag list is
checked against `packaging.tags` itself, and the cases below include every
tagging upstream has shipped torchaudio and torchvision with.
"""
from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wheel_tags as wt  # noqa: E402

VERSION = "2.11.0.dev20261008+cu134"


def wheel(tags: str, package: str = "torchaudio") -> str:
    return f"{package}-{VERSION}-{tags}.whl"


def interpreter(tags: str) -> wt.Interpreter:
    return wt.interpreter_of(wheel(tags, "torch"))


@pytest.mark.parametrize("minor", range(10, 16))
@pytest.mark.parametrize("free_threaded", [False, True], ids=["gil", "free-threaded"])
def test_supported_tags_are_the_ones_pip_generates(minor: int, free_threaded: bool) -> None:
    tags = pytest.importorskip("packaging.tags")
    version = pytest.importorskip("packaging.version")
    packaging = pytest.importorskip("packaging")
    if free_threaded and version.Version(packaging.__version__) < version.Version("24.0"):
        pytest.skip("packaging before 24.0 offers abi3 to free-threaded builds")
    abi = f"cp3{minor}" + ("t" if free_threaded else "")
    expected = {
        (t.interpreter, t.abi, t.platform)
        for t in itertools.chain(
            tags.cpython_tags((3, minor), abis=[abi], platforms=["win_arm64"]),
            tags.compatible_tags((3, minor), interpreter=f"cp3{minor}", platforms=["win_arm64"]),
        )
    }
    ours = wt.Interpreter(minor, free_threaded, "win_arm64").supported_tags()
    if version.Version(packaging.__version__) < version.Version("26.3"):
        # abi3t (PEP 803) arrived in packaging 26.3; older releases offer free-threaded builds no stable ABI.
        ours = frozenset(t for t in ours if t[1] != "abi3t")
    assert ours == expected


@pytest.mark.parametrize(
    "extension, cell, installs",
    [
        ("cp313-cp313-win_arm64", "cp313-cp313-win_arm64", True),
        ("cp312-cp312-win_arm64", "cp313-cp313-win_arm64", False),
        ("cp314-cp314-win_arm64", "cp314-cp314t-win_arm64", False),
        ("cp310-abi3-win_arm64", "cp313-cp313-win_arm64", True),
        ("cp313-abi3-win_arm64", "cp313-cp313-win_arm64", True),
        ("cp314-abi3-win_arm64", "cp313-cp313-win_arm64", False),
        ("cp310-abi3-win_arm64", "cp314-cp314t-win_arm64", False),
        ("cp315-abi3t-win_arm64", "cp315-cp315t-win_arm64", True),
        ("cp310-abi3t-win_arm64", "cp314-cp314t-win_arm64", True),
        ("cp316-abi3t-win_arm64", "cp315-cp315t-win_arm64", False),
        ("cp315-abi3t-win_arm64", "cp315-cp315-win_arm64", False),
        ("cp315-abi3.abi3t-win_arm64", "cp315-cp315-win_arm64", True),
        ("cp315-abi3.abi3t-win_arm64", "cp315-cp315t-win_arm64", True),
        ("py3-none-win_arm64", "cp311-cp311-win_arm64", True),
        ("py3-none-win_arm64", "cp314-cp314t-win_arm64", True),
        ("py313-none-win_arm64", "cp313-cp313-win_arm64", True),
        ("py314-none-win_arm64", "cp313-cp313-win_arm64", False),
        ("cp313-none-win_arm64", "cp314-cp314-win_arm64", False),
        ("py3-none-any", "cp313-cp313-win_arm64", True),
        ("py2.py3-none-any", "cp313-cp313-win_arm64", True),
        ("cp312.cp313-cp312.cp313-win_arm64", "cp313-cp313-win_arm64", True),
        ("py3-none-win_amd64", "cp313-cp313-win_arm64", False),
    ],
)
def test_installs(extension: str, cell: str, installs: bool) -> None:
    assert interpreter(cell).installs(wheel(extension)) is installs


def test_the_wheels_upstream_switched_to_on_2026_10_07_install_on_every_cell() -> None:
    """pytorch/audio#4234 and pytorch/vision#9643: one `py3-none` wheel for every Python."""
    cells = ["cp311-cp311", "cp312-cp312", "cp313-cp313", "cp314-cp314", "cp314-cp314t"]
    for cell in cells:
        target = interpreter(f"{cell}-win_arm64")
        assert target.installs("torchaudio-2.11.0.dev20261008+cu134-py3-none-win_arm64.whl"), cell
        assert target.installs("torchvision-0.30.0.dev20261008+cu134-py3-none-win_arm64.whl"), cell


@pytest.mark.parametrize(
    "tags, expected",
    [
        ("cp313-cp313-win_arm64", wt.Interpreter(13, False, "win_arm64")),
        ("cp314-cp314t-win_arm64", wt.Interpreter(14, True, "win_arm64")),
    ],
)
def test_interpreter_of_torch(tags: str, expected: wt.Interpreter) -> None:
    assert interpreter(tags) == expected
    assert str(expected) == tags


@pytest.mark.parametrize(
    "tags",
    ["py3-none-win_arm64", "cp310-abi3-win_arm64", "cp314t-cp314t-win_arm64", "cp312.cp313-cp312.cp313-win_arm64"],
)
def test_interpreter_of_refuses_a_wheel_not_built_for_one_interpreter(tags: str) -> None:
    with pytest.raises(ValueError, match="exactly one CPython interpreter"):
        interpreter(tags)


def test_a_build_tag_is_not_mistaken_for_a_tag() -> None:
    assert wt.tags_of(f"torch-{VERSION}-1-cp313-cp313-win_arm64.whl") == {("cp313", "cp313", "win_arm64")}


@pytest.mark.parametrize("name", ["torch.tar.gz", "torch-2.11.0-cp313-win_arm64.whl"])
def test_tags_of_refuses_a_non_wheel(name: str) -> None:
    with pytest.raises(ValueError, match="not a wheel filename"):
        wt.tags_of(name)
