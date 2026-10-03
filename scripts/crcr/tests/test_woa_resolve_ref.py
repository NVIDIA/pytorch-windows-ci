"""`resolve_ref` in the WoA orchestrator's `prep` step, run as written.

`prep` pins pytorch, torchaudio and torchvision to the SHAs every build and
test cell then uses, so a wrong pick is silent: the cells agree with each other
and build the wrong source. That happened. `git ls-remote <url> main` also
returns any ref that merely ends in `/main`, and pytorch/vision carries a stale
`refs/heads/<user>/main` that sorts ahead of `refs/heads/main`, so every WoA
nightly built torchvision from April 2025.

These tests lift the function out of the workflow and run it under bash with
`git` stubbed, so they check the shipped script rather than a copy of it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "windows-woa-build-test.yml"


def find_bash() -> str | None:
    if os.name != "nt":
        return shutil.which("bash")
    # On Windows, System32's bash.exe is the WSL launcher, which fails without a distro.
    git = shutil.which("git")
    if git is None:
        return None
    for root in Path(git).resolve().parents:
        if (bash := root / "usr" / "bin" / "bash.exe").exists():
            return str(bash)
    return None


BASH = find_bash()

pytestmark = pytest.mark.skipif(BASH is None, reason="needs bash")


def resolve_ref_source() -> str:
    prep = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["prep"]
    script = next(s["run"] for s in prep["steps"] if s.get("id") == "ref")
    match = re.search(r"^([ \t]*)resolve_ref\(\) \{\n.*?^\1\}$", script, re.MULTILINE | re.DOTALL)
    assert match, "resolve_ref() not found in prep's `ref` step"
    return textwrap.dedent(match.group(0))


def resolve(ref: str, *ls_remote: str) -> str:
    script = "\n".join([
        'git() { printf "%s" "${FAKE_LS_REMOTE}"; }',
        resolve_ref_source(),
        'resolve_ref "https://github.com/pytorch/vision" "$1"',
    ])
    env = {**os.environ, "FAKE_LS_REMOTE": "".join(f"{line}\n" for line in ls_remote)}
    proc = subprocess.run([BASH, "-c", script, "resolve_ref", ref], env=env, capture_output=True, check=True)
    return proc.stdout.decode()


A, B, C = "a" * 40, "b" * 40, "c" * 40


def test_a_branch_ending_in_the_name_does_not_shadow_the_branch() -> None:
    assert resolve("main", f"{A}\trefs/heads/Alexandre-SCHOEPP/main", f"{B}\trefs/heads/main") == B


def test_a_name_only_a_nested_branch_ends_in_resolves_to_nothing() -> None:
    assert resolve("release", f"{A}\trefs/heads/someone/release") == ""


def test_an_annotated_tag_resolves_to_its_commit() -> None:
    assert resolve("v1.0", f"{A}\trefs/tags/v1.0", f"{B}\trefs/tags/v1.0^{{}}") == B


def test_a_nested_annotated_tag_does_not_win_over_the_branch() -> None:
    assert resolve("v1.0", f"{A}\trefs/heads/v1.0", f"{B}\trefs/tags/old/v1.0^{{}}") == A


def test_a_lightweight_tag_resolves() -> None:
    assert resolve("v2", f"{C}\trefs/tags/v2") == C


def test_a_fully_qualified_ref_resolves() -> None:
    assert resolve("refs/heads/main", f"{A}\trefs/heads/other/refs/heads/main", f"{B}\trefs/heads/main") == B


def test_a_full_sha_passes_through_without_asking_the_remote() -> None:
    assert resolve(C, f"{A}\trefs/heads/main") == C
