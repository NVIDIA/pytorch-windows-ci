"""`resolve_ref.sh`, the source pin both WoA `prep` jobs share.

`prep` pins pytorch, torchaudio and torchvision to the SHAs every build and
test cell then uses, so a wrong pick is silent: the cells agree with each other
and build the wrong source. That happened. `git ls-remote <url> main` also
returns any ref that merely ends in `/main`, and pytorch/vision carries a stale
`refs/heads/<user>/main` that sorts ahead of `refs/heads/main`, so every WoA
nightly built torchvision from April 2025.

These tests source the script under bash with `git` stubbed, and check that
both workflows source this copy rather than carrying their own.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "source-refs" / "resolve_ref.sh"
WORKFLOWS = ROOT / ".github" / "workflows"
PREP_WORKFLOWS = ("windows-woa-build-test.yml", "_woa-pr-build-test.yml")


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
needs_bash = pytest.mark.skipif(BASH is None, reason="needs bash")


def run(snippet: str, ls_remote: tuple[str, ...] = (), *args: str) -> subprocess.CompletedProcess:
    script = "\n".join([
        'git() { printf "%s" "${FAKE_LS_REMOTE}"; }',
        f'source "{SCRIPT.as_posix()}"',
        snippet,
    ])
    env = {**os.environ, "FAKE_LS_REMOTE": "".join(f"{line}\n" for line in ls_remote)}
    return subprocess.run([BASH, "-c", script, "bash", *args], env=env, capture_output=True)


def resolve(ref: str, *ls_remote: str) -> str:
    proc = run('resolve_ref "https://github.com/pytorch/vision" "$1"', ls_remote, ref)
    assert proc.returncode == 0, proc.stderr.decode()
    return proc.stdout.decode()


A, B, C = "a" * 40, "b" * 40, "c" * 40


@needs_bash
def test_a_branch_ending_in_the_name_does_not_shadow_the_branch() -> None:
    assert resolve("main", f"{A}\trefs/heads/Alexandre-SCHOEPP/main", f"{B}\trefs/heads/main") == B


@needs_bash
def test_a_name_only_a_nested_branch_ends_in_resolves_to_nothing() -> None:
    assert resolve("release", f"{A}\trefs/heads/someone/release") == ""


@needs_bash
def test_an_annotated_tag_resolves_to_its_commit() -> None:
    assert resolve("v1.0", f"{A}\trefs/tags/v1.0", f"{B}\trefs/tags/v1.0^{{}}") == B


@needs_bash
def test_a_nested_annotated_tag_does_not_win_over_the_branch() -> None:
    assert resolve("v1.0", f"{A}\trefs/heads/v1.0", f"{B}\trefs/tags/old/v1.0^{{}}") == A


@needs_bash
def test_a_lightweight_tag_resolves() -> None:
    assert resolve("v2", f"{C}\trefs/tags/v2") == C


@needs_bash
def test_a_fully_qualified_ref_resolves() -> None:
    assert resolve("refs/heads/main", f"{A}\trefs/heads/other/refs/heads/main", f"{B}\trefs/heads/main") == B


@needs_bash
def test_a_full_sha_passes_through_without_asking_the_remote() -> None:
    assert resolve(C, f"{A}\trefs/heads/main") == C


@needs_bash
@pytest.mark.parametrize("sha, ok", [(A, True), ("", False), ("abc123", False)])
def test_require_sha_accepts_only_a_full_sha(sha: str, ok: bool) -> None:
    proc = run('require_sha torchvision "$1" main', (), sha)
    assert (proc.returncode == 0) is ok
    assert ("::error::" in proc.stderr.decode()) is not ok


@pytest.mark.parametrize("name", PREP_WORKFLOWS)
def test_each_prep_job_sources_the_shared_resolver(name: str) -> None:
    prep = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))["jobs"]["prep"]
    script = next(s["run"] for s in prep["steps"] if s.get("id") == "ref")
    assert "source scripts/source-refs/resolve_ref.sh" in script
    assert "resolve_ref() {" not in script and "require_sha() {" not in script
