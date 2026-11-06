# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""`Defaults.ps1` expands `{Key}` references while it loads the defaults.

These run the real loader: once on the repo's defaults, and on copies with one
extra .psd1 holding the case under test. Every PowerShell on PATH is used, so a
Windows box also covers Windows PowerShell 5.1, which the preflight actions run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
ENV_DIR = REPO / "tools" / "woa-build" / "shared" / "env"
SHELLS = [name for name in ("pwsh", "powershell") if shutil.which(name)]
LOAD = (
    "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
    "try { . $env:DEFAULTS_PS1 } catch { [Console]::Error.WriteLine($_.Exception.Message); exit 1 }; "
    "$Script:CiDefaultsTable | ConvertTo-Json -Depth 5 -Compress"
)
REFERENCE = re.compile(r"\{[A-Za-z][A-Za-z0-9]*\}")

pytestmark = pytest.mark.skipif(not SHELLS, reason="needs pwsh")


@pytest.fixture(scope="module", params=SHELLS)
def shell(request: pytest.FixtureRequest) -> str:
    return request.param


def load(shell: str, env_dir: Path) -> subprocess.CompletedProcess[str]:
    # An inherited PSModulePath from pwsh 7 hides Windows PowerShell's own modules
    # (Import-PowerShellDataFile included); each shell rebuilds its own when it is unset.
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    env["DEFAULTS_PS1"] = str(env_dir / "Defaults.ps1")
    return subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", LOAD],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )


def with_extra_defaults(tmp_path: Path, entries: str) -> Path:
    shutil.copy2(ENV_DIR / "Defaults.ps1", tmp_path)
    shutil.copytree(ENV_DIR / "defaults", tmp_path / "defaults")
    (tmp_path / "defaults" / "zz-under-test.psd1").write_text(
        f"@{{ Domain = 'UnderTest'; Defaults = @{{ {entries} }} }}\n", encoding="utf-8"
    )
    return tmp_path


@pytest.fixture(scope="module")
def defaults(shell: str) -> dict:
    result = load(shell, ENV_DIR)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_no_reference_survives_the_load(defaults: dict) -> None:
    left = {k: v for k, v in defaults.items() if isinstance(v, str) and REFERENCE.search(v)}
    assert not left


def test_toolchain_paths_are_built_from_the_version_keys(defaults: dict) -> None:
    cuda, root = defaults["CudaVersion"], defaults["CudnnRoot"]
    assert defaults["CudaPath"] == rf"C:\Program Files\NVIDIA\CUDA\v{cuda}"
    assert root == rf"C:\Program Files\NVIDIA\CUDNN\v{defaults['CudnnVersion']}"
    assert defaults["CudnnLibDir"] == rf"{root}\lib\{cuda}\arm64"
    assert defaults["CudnnIncludeDir"] == rf"{root}\include\{cuda}"
    assert defaults["CudnnBinDir"] == rf"{root}\bin\{cuda}\arm64"
    assert defaults["VcvarsBat"] == rf"{defaults['VsBuildToolsRoot']}\VC\Auxiliary\Build\vcvarsarm64.bat"


def test_references_chain_across_files(shell: str, tmp_path: Path) -> None:
    result = load(shell, with_extra_defaults(tmp_path, "Outer = 'o-{Inner}'; Inner = 'i-{CudaVersion}'"))
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    assert loaded["Outer"] == f"o-i-{loaded['CudaVersion']}"


@pytest.mark.parametrize(
    ("entries", "message"),
    [
        ("Dangling = '{NoSuchKey}'", "Dangling in zz-under-test.psd1 references unknown default '{NoSuchKey}'"),
        ("LoopSelf = 'x{LoopSelf}'", "circular {Key} reference: LoopSelf -> LoopSelf"),
        ("LoopA = '{LoopB}'; LoopB = '{LoopA}'", "circular {Key} reference"),
    ],
    ids=["unknown", "self", "two-key-cycle"],
)
def test_bad_references_fail_the_load(shell: str, tmp_path: Path, entries: str, message: str) -> None:
    result = load(shell, with_extra_defaults(tmp_path, entries))
    assert result.returncode != 0
    assert message in result.stderr
