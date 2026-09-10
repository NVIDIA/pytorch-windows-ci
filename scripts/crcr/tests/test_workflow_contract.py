"""Contract tests between the orchestrators and the HUD reporting jobs.

The reporting jobs rename things by hand: they rebuild cell names from a label
list, and they glob for artifacts by a pattern assembled from matrix values.
Nothing in GitHub Actions checks that those strings still agree with the jobs
and uploads they are meant to describe, and the failure is quiet - a pattern
that matches nothing yields a cell with no evidence, and a label list that has
drifted from the matrix either reports the wrong set or aborts at run time.

These tests read the workflows and assert the couplings hold, so the mismatch
surfaces in lint rather than in a nightly.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"

RUN_ID, RUN_ATTEMPT = "17", "2"


def load(name: str) -> dict:
    # `yaml.safe_load` resolves the `&config` / `*arch` anchors the
    # orchestrators use to keep their matrices in step, which is exactly the
    # view these tests need.
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def steps_of(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job]["steps"]


def find_step(steps: list[dict], predicate) -> dict:
    matches = [s for s in steps if predicate(s)]
    assert len(matches) == 1, f"expected exactly one matching step, got {len(matches)}"
    return matches[0]


def upload_artifact_template(reusable: str) -> str:
    """The `name:` of the test-reports upload in a reusable test workflow."""
    wf = load(reusable)
    for job in wf["jobs"].values():
        for step in job.get("steps", []) or []:
            if "upload-artifact" not in str(step.get("uses", "")):
                continue
            name = str(step.get("with", {}).get("name", ""))
            if name.startswith("test-reports-"):
                return name
    raise AssertionError(f"no test-reports upload found in {reusable}")


def substitute(template: str, values: dict[str, str]) -> str:
    """Resolve `${{ ... }}` placeholders, failing loudly on an unknown one."""

    def replace(match: re.Match) -> str:
        expr = match.group(1).strip()
        if expr not in values:
            raise AssertionError(f"unhandled expression in template: ${{{{ {expr} }}}}")
        return values[expr]

    return re.sub(r"\$\{\{(.+?)\}\}", replace, template)


# --------------------------------------------------------------------------
# WoA: the label list drives both HUD rows, so it must equal the live matrix.
# --------------------------------------------------------------------------


def test_woa_python_labels_match_the_enabled_matrix():
    """`PYTHON_LABELS` names every enabled cell, and only enabled cells.

    A commented-out matrix entry produces no job at all, so a label left in
    the list makes `resolve_cell_conclusion.py` abort (it refuses to guess),
    and a cell enabled but unlisted is silently dropped from its HUD row.
    """
    wf = load("windows-woa-build-test.yml")
    declared = str(wf["env"]["PYTHON_LABELS"]).split()
    enabled = [c["label"] for c in wf["jobs"]["build"]["strategy"]["matrix"]["config"]]

    assert declared == enabled, (
        "PYTHON_LABELS and the build matrix have drifted.\n"
        f"  PYTHON_LABELS: {declared}\n"
        f"  matrix labels: {enabled}\n"
        "Enabling or disabling a Python cell must update both, in one commit."
    )


def test_woa_build_and_test_matrices_are_the_same_cells():
    """Both HUD rows are built from one label list, so one matrix must serve both."""
    wf = load("windows-woa-build-test.yml")
    build = [c["label"] for c in wf["jobs"]["build"]["strategy"]["matrix"]["config"]]
    test = [c["label"] for c in wf["jobs"]["test"]["strategy"]["matrix"]["config"]]
    assert build == test


def test_woa_shard_count_comes_from_prep():
    """The fan-out and `--expected-shards` must read the same number.

    If the test job fell back to `_woa-test.yml`'s own default, that default
    could move without `prep` following, and the reporting job would expect
    fewer shards than ran - silently losing the missing-shard check.
    """
    wf = load("windows-woa-build-test.yml")
    passed = str(wf["jobs"]["test"]["with"]["num-shards"])
    assert "needs.prep.outputs.num-shards" in passed, (
        f"test job passes num-shards as {passed!r}; it should come from prep"
    )
    assert "needs.prep.outputs.num-shards" in str(
        wf["jobs"]["report-test-crcr"]["steps"]
    ), "report-test-crcr should take --expected-shards from the same prep output"


# --------------------------------------------------------------------------
# WoA: one row aggregates every version, so the glob and the group regex have
# to agree with what `_woa-test.yml` actually uploads.
# --------------------------------------------------------------------------


def woa_artifact_names(labels: list[str], shards: int) -> list[str]:
    template = upload_artifact_template("_woa-test.yml")
    wf = load("windows-woa-build-test.yml")
    build_env = wf["jobs"]["test"]["with"]["build-environment"]
    return [
        substitute(
            template,
            {
                "inputs.build-environment": build_env,
                "inputs.python-label": label,
                "matrix.shard": str(shard),
                "github.run_id": RUN_ID,
                "github.run_attempt": RUN_ATTEMPT,
            },
        )
        for label in labels
        for shard in range(1, shards + 1)
    ]


def woa_reporting_step(step_id: str) -> dict:
    wf = load("windows-woa-build-test.yml")
    return find_step(
        steps_of(wf, "report-test-crcr"), lambda s: s.get("id") == step_id
    )


def test_woa_download_pattern_matches_every_shard_upload():
    download = woa_reporting_step("download")
    pattern = substitute(
        str(download["with"]["pattern"]),
        {"github.run_id": RUN_ID, "github.run_attempt": RUN_ATTEMPT},
    )
    names = woa_artifact_names(["py311", "py313", "py314", "py314t"], shards=4)
    assert names, "no artifact names generated"
    for name in names:
        assert fnmatch.fnmatch(name, pattern), (
            f"reporting job would not download {name!r}\n  pattern: {pattern!r}"
        )


def test_woa_download_pattern_excludes_other_runs_and_attempts():
    """The pattern is the only thing keeping a sibling run's reports out."""
    download = woa_reporting_step("download")
    pattern = substitute(
        str(download["with"]["pattern"]),
        {"github.run_id": RUN_ID, "github.run_attempt": RUN_ATTEMPT},
    )
    template = upload_artifact_template("_woa-test.yml")
    wf = load("windows-woa-build-test.yml")
    foreign = substitute(
        template,
        {
            "inputs.build-environment": wf["jobs"]["test"]["with"]["build-environment"],
            "inputs.python-label": "py313",
            "matrix.shard": "1",
            "github.run_id": "999999",
            "github.run_attempt": RUN_ATTEMPT,
        },
    )
    assert not fnmatch.fnmatch(foreign, pattern)


def test_woa_group_regex_recovers_the_python_label():
    """Grouped aggregation depends on reading the version back off the dir name.

    If this stops matching, every shard lands in one unlabelled group and the
    versions are unioned on test identity again - understating the totals sent
    upstream by roughly the number of cells.
    """
    summarize = woa_reporting_step("results")
    run = str(summarize["run"])
    match = re.search(r"--group-regex\s+'([^']+)'", run)
    assert match, "could not find --group-regex in the summarize step"
    group_re = re.compile(match.group(1))

    labels = ["py311", "py312", "py313", "py314", "py314t"]
    for name in woa_artifact_names(labels, shards=4):
        found = group_re.search(name)
        assert found, f"group regex did not match {name!r}"
        expected = next(l for l in labels if f"-{l}-" in name)
        assert found.group("group") == expected, (
            f"{name!r} -> {found.group('group')!r}, expected {expected!r}"
        )


def test_woa_group_regex_keeps_py314_and_py314t_apart():
    """`py314` is a prefix of `py314t`; the row must not merge them."""
    summarize = woa_reporting_step("results")
    match = re.search(r"--group-regex\s+'([^']+)'", str(summarize["run"]))
    group_re = re.compile(match.group(1))

    seen = {
        group_re.search(name).group("group")
        for name in woa_artifact_names(["py314", "py314t"], shards=1)
    }
    assert seen == {"py314", "py314t"}


# --------------------------------------------------------------------------
# RTX: one row per cell, so each pattern must be scoped to its own cell.
# --------------------------------------------------------------------------


def rtx_cells() -> list[tuple[dict, dict]]:
    wf = load("windows-rtx-build-test.yml")
    matrix = wf["jobs"]["report-test-crcr"]["strategy"]["matrix"]
    return [(c, a) for c in matrix["config"] for a in matrix["arch"]]


def rtx_artifact_name(config: dict, arch: dict, shard: int) -> str:
    template = upload_artifact_template("_rtx-test.yml")
    wf = load("windows-rtx-build-test.yml")
    build_env = substitute(
        str(wf["jobs"]["test"]["with"]["build-environment"]),
        {"matrix.arch.name": arch["name"]},
    )
    return substitute(
        template,
        {
            "inputs.build-environment": build_env,
            "inputs.python-label": config["python"]["label"],
            "inputs.cuda-label": config["cuda"]["label"],
            "inputs.arch-name": arch["name"],
            "matrix.shard": str(shard),
            "github.run_id": RUN_ID,
            "github.run_attempt": RUN_ATTEMPT,
        },
    )


def rtx_download_pattern(config: dict, arch: dict) -> str:
    wf = load("windows-rtx-build-test.yml")
    download = find_step(
        steps_of(wf, "report-test-crcr"), lambda s: s.get("id") == "download"
    )
    return substitute(
        str(download["with"]["pattern"]),
        {
            "matrix.arch.name": arch["name"],
            "matrix.config.python.label": config["python"]["label"],
            "matrix.config.cuda.label": config["cuda"]["label"],
            "github.run_id": RUN_ID,
            "github.run_attempt": RUN_ATTEMPT,
        },
    )


@pytest.mark.parametrize("config,arch", rtx_cells())
def test_rtx_download_pattern_matches_its_own_shards(config, arch):
    pattern = rtx_download_pattern(config, arch)
    for shard in range(1, 6):
        name = rtx_artifact_name(config, arch, shard)
        assert fnmatch.fnmatch(name, pattern), (
            f"cell would not download its own {name!r}\n  pattern: {pattern!r}"
        )


def test_rtx_download_patterns_do_not_cross_cells():
    """Six rows means six disjoint globs; an overlap would double-count."""
    cells = rtx_cells()
    assert len(cells) > 1
    for config, arch in cells:
        pattern = rtx_download_pattern(config, arch)
        for other_config, other_arch in cells:
            if (other_config, other_arch) == (config, arch):
                continue
            for shard in range(1, 6):
                foreign = rtx_artifact_name(other_config, other_arch, shard)
                assert not fnmatch.fnmatch(foreign, pattern), (
                    f"{pattern!r} also matches another cell's {foreign!r}"
                )
