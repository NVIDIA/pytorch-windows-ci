# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""The WoA toolchain versions are named in one place: `build-toolchain.psd1`.

The CUDA, cuDNN and VS BuildTools install paths used to be repeated in every
WoA workflow, both preflight actions and the defaults file, so a cuDNN bump
touched eleven files, and a missed copy left the build and its preflight check
looking at different installs. The workflows, actions and woa-build scripts now
read the paths from `build-toolchain.psd1`, whose paths are built from one key
per version through `{Key}` references that `Defaults.ps1` expands. These tests
keep it that way.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
DEFAULTS_DIR = REPO / "tools" / "woa-build" / "shared" / "env" / "defaults"
TOOLCHAIN = DEFAULTS_DIR / "build-toolchain.psd1"
WOA_YAML = sorted(
    [*(REPO / ".github" / "workflows").glob("*woa*.yml"), *(REPO / ".github" / "actions").glob("woa-*/action.yml")]
)
WOA_SCRIPTS = sorted(
    p for p in (REPO / "tools" / "woa-build").rglob("*") if p.suffix in (".ps1", ".psd1") and p != TOOLCHAIN
)
# A versioned install path, either slash: ...\NVIDIA\CUDA\v13.4, .../NVIDIA/CUDNN/v9.27,
# ...\Microsoft Visual Studio\18\...
VERSIONED_PATH = re.compile(r"NVIDIA[\\/](?:CUDA|CUDNN)[\\/]v\d|Microsoft Visual Studio[\\/]\d+[\\/]", re.IGNORECASE)
# `Key = 'value'` / `Key = "value"` lines of a defaults .psd1 (string values only; comments never match).
ASSIGNMENT = re.compile(r"""^\s*(\w+)\s*=\s*(['"])(.*?)\2""", re.MULTILINE)
REFERENCE = re.compile(r"\{([A-Za-z][A-Za-z0-9]*)\}")
VERSION_KEYS = ("CudaVersion", "CudnnVersion", "VsBuildToolsRoot")


def string_defaults(path: Path) -> dict[str, str]:
    return {key: value for key, _quote, value in ASSIGNMENT.findall(path.read_text(encoding="utf-8"))}


@pytest.fixture(scope="module")
def toolchain() -> dict[str, str]:
    return string_defaults(TOOLCHAIN)


def test_the_scanned_file_sets_are_not_empty() -> None:
    assert len(WOA_YAML) >= 6, [p.name for p in WOA_YAML]
    assert len(WOA_SCRIPTS) >= 40, [p.name for p in WOA_SCRIPTS]


@pytest.mark.parametrize("path", WOA_YAML + WOA_SCRIPTS, ids=lambda p: p.relative_to(REPO).as_posix())
def test_nothing_else_hard_codes_a_toolchain_path(path: Path) -> None:
    hits = [
        f"{path.name}:{n}: {line.strip()}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if VERSIONED_PATH.search(line)
    ]
    assert not hits, (
        "toolchain install paths belong in tools/woa-build/shared/env/defaults/build-toolchain.psd1 "
        "(read them with Resolve-CiEnv / Get-CiDefault):\n" + "\n".join(hits)
    )


@pytest.mark.parametrize("key", VERSION_KEYS)
def test_each_toolchain_version_is_written_once(toolchain: dict[str, str], key: str) -> None:
    pin = toolchain[key]
    repeats = [k for k, v in toolchain.items() if k != key and pin in v]
    assert not repeats, f"{repeats} repeat {key}'s value {pin!r}; reference it as {{{key}}} instead"


def test_versioned_paths_are_built_from_the_version_keys(toolchain: dict[str, str]) -> None:
    versioned = {k: v for k, v in toolchain.items() if k not in VERSION_KEYS and VERSIONED_PATH.search(v)}
    assert not versioned, f"spell these with {{CudaVersion}} / {{CudnnVersion}} / {{VsBuildToolsRoot}}: {versioned}"


def test_every_reference_names_a_default() -> None:
    every = {}
    for path in sorted(DEFAULTS_DIR.glob("*.psd1")):
        every.update({k: (path.name, v) for k, v in string_defaults(path).items()})
    dangling = [
        f"{owner}: {key} -> {{{ref}}}"
        for key, (owner, value) in every.items()
        for ref in REFERENCE.findall(value)
        if ref not in every
    ]
    assert not dangling, dangling
