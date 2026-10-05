# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Contract tests for the WoA wheel signing + publication workflows.

Publication is the one part of this repo that is not reversible. A wheel pushed
to the `nvtorch_oot` devzone is public under NVIDIA's name, and the guards that
stop that happening by accident are spread across workflow triggers and `if:`s,
job permissions, environment names, and artifact names that seven workflows and
a resolver script must agree on. GitHub checks none of those couplings.

These tests pin the properties that must survive any future edit:

  * a pull-request run can never sign or publish - publication is called only
    by the nightly and, where a repository carries it, by the manual
    re-publication workflow; a PR can reach neither, and the pipeline the PR
    path does call requests no publication scope at all
  * the nightly publishes only when opted in, and every input defaults to the
    reversible path
  * signing credentials exist only on the hosted signing job, and Kitmaker
    credentials only on hosted publication jobs - never on the persistent
    self-hosted WoA runners
  * the artifact and job names one piece writes are the ones the next one reads,
    including when the build run and the publication run are the same run
  * production cannot run without its dry run
"""

from __future__ import annotations

import fnmatch
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
PUBLISH_SCRIPTS = ROOT / "scripts" / "publish"

sys.path.insert(0, str(PUBLISH_SCRIPTS))

import build_run  # noqa: E402

NIGHTLY = "windows-woa-build-test.yml"
DISPATCH = "windows-woa-publish.yml"
SIGN_PUBLISH = "_woa-sign-publish.yml"
PR_PIPELINE = "_woa-pr-build-test.yml"
UPSTREAM_PULL = "upstream-pull.yml"
BUILD_CELL = "_woa-build.yml"
SIGN = "_woa-sign.yml"
PUBLISH = "_woa-publish.yml"
VERIFY = "_woa-verify.yml"
BUILD_RUN_ID, PUBLISH_RUN_ID = "900", "4242"
# (build run, publication run): windows-woa-publish.yml re-publishing an earlier
# run, and the nightly publishing itself.
RUN_PAIRS = [(BUILD_RUN_ID, PUBLISH_RUN_ID), (PUBLISH_RUN_ID, PUBLISH_RUN_ID)]
CALLER_JOB = {NIGHTLY: "publication", DISPATCH: "publication"}
# A repository that forbids manual runs carries no windows-woa-publish.yml, and
# its nightly offers no `workflow_dispatch`.
HAS_DISPATCH = (WORKFLOWS / DISPATCH).is_file()
CALLERS = [NIGHTLY, DISPATCH] if HAS_DISPATCH else [NIGHTLY]
needs_dispatch = pytest.mark.skipif(not HAS_DISPATCH, reason=f"this repository carries no {DISPATCH}")


def load(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def on(workflow: dict) -> dict:
    # PyYAML parses the bare key `on` as the boolean True.
    return workflow[True]


needs_nightly_dispatch = pytest.mark.skipif(
    "workflow_dispatch" not in on(load(NIGHTLY)), reason="this copy of the nightly has no manual trigger")


def squash(text) -> str:
    return " ".join(str(text).split())


def steps(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job].get("steps", []) or []


def step_named(workflow: dict, job: str, name: str) -> dict:
    matches = [s for s in steps(workflow, job) if s.get("name") == name]
    assert len(matches) == 1, f"expected one step {name!r} in {job}, got {len(matches)}"
    return matches[0]


def uses(workflow: dict, job: str, action: str) -> list[dict]:
    return [s for s in steps(workflow, job) if str(s.get("uses", "")).startswith(action)]


def resolve(template: str, label: str = "py313", build_run_id: str = BUILD_RUN_ID,
            run_id: str = PUBLISH_RUN_ID) -> str:
    return (str(template)
            .replace("${{ matrix.config.label }}", label)
            .replace("${{ needs.resolve.outputs.build-run-id }}", build_run_id)
            .replace("${{ github.run_id }}", run_id))


def evaluate(expression: str, context: dict[str, object]) -> object:
    """Evaluate a GitHub Actions expression over `context`, keyed by dotted path.

    Covers the subset these workflows use: `&&`, `||`, `!`, `==`, `!=`, string
    literals, and `cancelled()`. A context path missing from `context` is null,
    as GitHub treats an undeclared input or unset variable.
    """
    body = squash(expression)
    if body.startswith("${{") and body.endswith("}}"):
        body = body[3:-2]
    body = re.sub(
        r"\b(?:github|inputs|vars|needs)(?:\.[\w-]+)+",
        lambda m: repr(context.get(m.group(0))),
        body,
    )
    body = body.replace("cancelled()", "False").replace("&&", " and ").replace("||", " or ")
    body = re.sub(r"!(?!=)", " not ", body)
    return eval(body, {"__builtins__": {}}, {})  # noqa: S307 - our own workflow text


def uses_targets(workflow: dict) -> set[str]:
    return {str(j.get("uses", "")).rsplit("/", 1)[-1] for j in workflow["jobs"].values() if "uses" in j}


def requested_permissions(name: str) -> dict[str, str]:
    """Every permission `name`, its jobs, or any workflow they call asks for, strongest wins."""
    rank = {"none": 0, "read": 1, "write": 2}
    workflow = load(name)
    wanted: dict[str, str] = {}
    blocks = [workflow.get("permissions") or {}]
    blocks += [j.get("permissions") or {} for j in workflow["jobs"].values()]
    blocks += [requested_permissions(t) for t in uses_targets(workflow)]
    for block in blocks:
        for scope, level in block.items():
            if rank[str(level)] > rank[wanted.get(scope, "none")]:
                wanted[scope] = str(level)
    return wanted


@pytest.fixture(scope="module")
def nightly() -> dict:
    return load(NIGHTLY)


@pytest.fixture(scope="module")
def dispatch() -> dict:
    return load(DISPATCH)


@pytest.fixture(scope="module")
def flow() -> dict:
    return load(SIGN_PUBLISH)


@pytest.fixture(scope="module")
def sign() -> dict:
    return load(SIGN)


@pytest.fixture(scope="module")
def publish() -> dict:
    return load(PUBLISH)


@pytest.fixture(scope="module")
def verify() -> dict:
    return load(VERIFY)


# --------------------------------------------------------------------------
# A PR run must not be able to sign or publish.
# --------------------------------------------------------------------------


def test_publication_is_called_only_by_its_entry_points() -> None:
    callers: dict[str, set[str]] = {}
    for path in WORKFLOWS.glob("*.yml"):
        for target in uses_targets(load(path.name)):
            callers.setdefault(target, set()).add(path.name)
    assert callers[SIGN_PUBLISH] == set(CALLERS)
    for stage in (SIGN, PUBLISH, VERIFY):
        assert callers[stage] == {SIGN_PUBLISH}, stage


def test_no_caller_is_reachable_from_a_pull_request(nightly: dict) -> None:
    """No `workflow_call` either: anything that could call them would inherit publication."""
    assert set(on(nightly)) <= {"schedule", "workflow_dispatch"}
    if HAS_DISPATCH:
        assert set(on(load(DISPATCH))) == {"workflow_dispatch"}


def test_the_pr_path_calls_the_pr_pipeline_and_nothing_that_publishes() -> None:
    pull = load(UPSTREAM_PULL)
    assert pull["jobs"]["woa"]["uses"] == f"./.github/workflows/{PR_PIPELINE}"
    assert not uses_targets(pull) & {NIGHTLY, DISPATCH, SIGN_PUBLISH, SIGN, PUBLISH, VERIFY}


def test_the_pr_pipeline_requests_no_publication_scope() -> None:
    """GitHub makes the caller grant every permission any nested job requests, so
    nothing under the PR pipeline may ask for write access the PR path would have to grant."""
    requested = requested_permissions(PR_PIPELINE)
    assert "write" not in requested.values(), requested
    assert not uses_targets(load(PR_PIPELINE)) & {SIGN_PUBLISH, SIGN, PUBLISH, VERIFY}


def test_the_publish_gate_refuses_untrusted_events_itself(publish: dict) -> None:
    script = step_named(publish, "gate", "Validate the publication request")["run"]
    assert "schedule|workflow_dispatch) ;;" in script


def test_the_resolver_refuses_relayed_pr_builds() -> None:
    assert "repository_dispatch" not in build_run.TRUSTED_EVENTS
    assert set(build_run.TRUSTED_EVENTS) == {"schedule", "workflow_dispatch"}


# --------------------------------------------------------------------------
# The nightly publishes only when opted in.
# --------------------------------------------------------------------------


def nightly_publication(nightly: dict, **context: object) -> dict | None:
    """What the nightly's `publication` job would be called with, or None if it is skipped.

    An input the job does not pass takes `_woa-sign-publish.yml`'s default.
    """
    context = {"needs.build.result": "success", **context}
    job = nightly["jobs"]["publication"]
    if not evaluate(job["if"], context):
        return None
    declared = on(load(SIGN_PUBLISH))["workflow_call"]["inputs"]
    called = {}
    for key in ("channel", "run-kitmaker-dry-run", "run-kitmaker-release", "kitmaker-probe"):
        value = job["with"].get(key, declared[key].get("default"))
        called[key] = evaluate(value, context) if "${{" in str(value) else value
    return called


def scheduled(setting: str | None, **context: object) -> dict[str, object]:
    return {"github.event_name": "schedule", "vars.WOA_NIGHTLY_PUBLISH": setting, **context}


def dispatched(channel: str = "none", kitmaker: str = "none", **context: object) -> dict[str, object]:
    return {"github.event_name": "workflow_dispatch",
            "inputs.publish-channel": channel, "inputs.kitmaker": kitmaker, **context}


NOTHING_ELSE = {"run-kitmaker-dry-run": False, "run-kitmaker-release": False, "kitmaker-probe": False}


@pytest.mark.parametrize("setting", [None, "", "off", "true", "dry_run"])
def test_a_scheduled_run_publishes_nothing_unless_opted_in(nightly: dict, setting: str | None) -> None:
    assert nightly_publication(nightly, **scheduled(setting)) is None


def test_a_dry_run_night_releases_on_github_and_stops_at_the_kitmaker_dry_run(nightly: dict) -> None:
    assert nightly_publication(nightly, **scheduled("dry-run")) == \
        {"channel": "nightly", "run-kitmaker-dry-run": True, "run-kitmaker-release": False, "kitmaker-probe": False}


def test_a_release_night_publishes_to_pypi(nightly: dict) -> None:
    assert nightly_publication(nightly, **scheduled("release")) == \
        {"channel": "nightly", "run-kitmaker-dry-run": True, "run-kitmaker-release": True, "kitmaker-probe": False}


def test_a_dispatch_publishes_nothing_by_default(nightly: dict) -> None:
    assert nightly_publication(nightly, **dispatched(**{"vars.WOA_NIGHTLY_PUBLISH": "release"})) is None


@needs_nightly_dispatch
@pytest.mark.parametrize("kitmaker, expected", [
    ("none", NOTHING_ELSE),
    ("probe", {**NOTHING_ELSE, "kitmaker-probe": True}),
    ("dry-run", {**NOTHING_ELSE, "run-kitmaker-dry-run": True}),
    ("release", {**NOTHING_ELSE, "run-kitmaker-dry-run": True, "run-kitmaker-release": True}),
])
def test_a_dispatch_does_exactly_what_it_asks(nightly: dict, kitmaker: str, expected: dict) -> None:
    """The repository variable binds the schedule only: it must not upgrade a dispatch."""
    context = dispatched("nightly", kitmaker, **{"vars.WOA_NIGHTLY_PUBLISH": "release"})
    assert nightly_publication(nightly, **context) == {"channel": "nightly", **expected}


@needs_nightly_dispatch
def test_a_rehearsal_dispatch_is_a_rehearsal(nightly: dict) -> None:
    assert nightly_publication(nightly, **dispatched("rehearsal")) == {"channel": "rehearsal", **NOTHING_ELSE}


@pytest.mark.parametrize("event", ["repository_dispatch", "pull_request", "workflow_call", "push"])
def test_no_other_event_publishes(nightly: dict, event: str) -> None:
    context = {**scheduled("release"), **dispatched("nightly", "release"), "github.event_name": event}
    assert nightly_publication(nightly, **context) is None


def test_publication_follows_the_builds_not_the_tests(nightly: dict) -> None:
    """Cells that built publish when another cell failed; nothing publishes when the
    build stage never ran. The tests have no say - build_run.py judges each cell."""
    job = nightly["jobs"]["publication"]
    assert job["needs"] == "build"
    assert "conclusion" not in squash(job["if"])
    assert nightly_publication(nightly, **scheduled("release", **{"needs.build.result": "failure"}))
    assert nightly_publication(nightly, **scheduled("release", **{"needs.build.result": "skipped"})) is None


def test_the_nightly_publishes_its_own_run(nightly: dict) -> None:
    assert nightly["jobs"]["publication"]["with"]["build-run-id"] == "${{ github.run_id }}"


# --------------------------------------------------------------------------
# Safe defaults.
# --------------------------------------------------------------------------


@needs_dispatch
@pytest.mark.parametrize("name", ["run-kitmaker-dry-run", "run-kitmaker-release", "kitmaker-probe"])
def test_dispatch_publishing_inputs_default_off(dispatch: dict, name: str) -> None:
    assert on(dispatch)["workflow_dispatch"]["inputs"][name]["default"] is False


@needs_dispatch
def test_the_default_channel_is_a_draft_rehearsal(dispatch: dict) -> None:
    channel = on(dispatch)["workflow_dispatch"]["inputs"]["publish-channel"]
    assert channel["default"] == "rehearsal"
    assert set(channel["options"]) == {"rehearsal", "nightly", "release"}


@needs_nightly_dispatch
def test_a_nightly_dispatch_defaults_to_publishing_nothing(nightly: dict) -> None:
    inputs = on(nightly)["workflow_dispatch"]["inputs"]
    assert inputs["publish-channel"]["default"] == "none"
    # The release channel is for windows-woa-publish.yml, which takes a justification.
    assert set(inputs["publish-channel"]["options"]) == {"none", "rehearsal", "nightly"}
    assert inputs["kitmaker"]["default"] == "none"


@pytest.mark.parametrize("workflow", [SIGN_PUBLISH, PUBLISH])
@pytest.mark.parametrize("name", ["run-kitmaker-dry-run", "run-kitmaker-release", "kitmaker-probe"])
def test_reusable_publish_inputs_fail_closed(workflow: str, name: str) -> None:
    assert on(load(workflow))["workflow_call"]["inputs"][name]["default"] is False


# --------------------------------------------------------------------------
# Who holds which credential.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("caller", CALLERS)
def test_callers_grant_exactly_what_the_publication_uses(caller: str) -> None:
    """A called workflow can only narrow the caller's token, so these grants are
    required, and nothing beyond them should be granted - the rest of each caller
    keeps a read-only token."""
    workflow = load(caller)
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    job = workflow["jobs"][CALLER_JOB[caller]]
    assert job["permissions"] == SIGN_PERMISSIONS | {"contents": "write"}
    assert requested_permissions(SIGN_PUBLISH) == job["permissions"]


SIGN_PERMISSIONS = {"contents": "read", "id-token": "write", "actions": "read", "attestations": "write"}


def test_the_publication_jobs_grant_their_stages_exactly_what_they_use(flow: dict) -> None:
    jobs = flow["jobs"]
    assert flow["permissions"] == {"contents": "read", "actions": "read"}
    assert jobs["sign"]["permissions"] == SIGN_PERMISSIONS
    assert jobs["publish"]["permissions"] == {"contents": "write", "id-token": "write", "attestations": "read"}
    assert "permissions" not in jobs["resolve"] and "permissions" not in jobs["verify"]


@pytest.mark.parametrize("caller", CALLERS)
def test_a_build_run_is_never_published_twice_at_once(caller: str) -> None:
    job = load(caller)["jobs"][CALLER_JOB[caller]]
    assert job["concurrency"] == {
        "group": f"windows-woa-publish-{job['with']['build-run-id']}",
        "cancel-in-progress": False,
    }


def test_signing_runs_hosted_under_its_own_environment(sign: dict) -> None:
    job = sign["jobs"]["sign"]
    assert job["runs-on"] == "windows-2025"
    assert job["environment"] == "woa-signing"
    assert job["permissions"] == SIGN_PERMISSIONS


def test_validation_on_woa_runners_holds_no_credentials(sign: dict) -> None:
    job = sign["jobs"]["validate"]
    assert job["runs-on"] == ["${{ inputs.runner-base }}"]
    assert "environment" not in job and "permissions" not in job
    assert "secrets." not in yaml.safe_dump(job)


def test_every_publication_job_is_hosted(publish: dict) -> None:
    """The Kitmaker token and the release-writing token never reach the persistent WoA runners."""
    for name, job in publish["jobs"].items():
        assert job["runs-on"] == "ubuntu-latest", f"{name} must run on a GitHub-hosted runner"


def test_publication_jobs_narrow_their_permissions(publish: dict) -> None:
    jobs = publish["jobs"]
    assert jobs["github-release"]["permissions"] == {"contents": "write", "attestations": "read"}
    assert jobs["github-release"]["environment"] == "woa-publish-${{ inputs.channel }}"
    for name in ("kitmaker-probe", "kitmaker-dry-run", "kitmaker-release"):
        assert jobs[name]["permissions"] == {"contents": "read", "id-token": "write"}, name
        assert jobs[name]["environment"] == "woa-publish-${{ inputs.channel }}"


def test_verification_on_woa_runners_holds_no_credentials(verify: dict) -> None:
    job = verify["jobs"]["verify"]
    assert "environment" not in job and "permissions" not in job
    assert "secrets." not in yaml.safe_dump(job)


@pytest.mark.parametrize("name", [SIGN, VERIFY])
def test_self_hosted_jobs_scrub_the_workspace_folders_they_use(name: str) -> None:
    """The workspace root outlives the job on a persistent runner, so a folder an artifact
    step reads or writes still holds the previous run's files when the next job starts -
    which is how one validation run saw another run's wheels."""
    workflow = load(name)
    self_hosted = [j for j, spec in workflow["jobs"].items() if "inputs.runner-base" in str(spec.get("runs-on"))]
    assert self_hosted, name
    for job in self_hosted:
        used = {
            str(s["with"]["path"]).split("/")[0]
            for s in steps(workflow, job)
            if str(s.get("uses", "")).startswith(("actions/download-artifact@", "actions/upload-artifact@"))
        }
        assert used, f"{name}:{job}"
        for phase in ("pre", "post"):
            clean = [s for s in uses(workflow, job, "./oot/.github/actions/woa-strict-clean")
                     if s["with"]["phase"] == phase]
            assert len(clean) == 1, f"{name}:{job} has no single {phase} strict clean"
            scrubbed = {p.strip().removeprefix("${{ github.workspace }}/")
                        for p in str(clean[0]["with"].get("extra-paths", "")).splitlines() if p.strip()}
            assert used <= scrubbed, f"{name}:{job} {phase} clean misses {sorted(used - scrubbed)}"


# --------------------------------------------------------------------------
# Signing and validation.
# --------------------------------------------------------------------------


def test_signing_covers_every_native_extension_and_timestamps(sign: dict) -> None:
    action = uses(sign, "sign", "azure/artifact-signing-action@")
    assert len(action) == 1
    config = action[0]["with"]
    assert set(config["files-folder-filter"].split(",")) == {"dll", "pyd", "exe", "node"}
    assert str(config["files-folder-recurse"]).lower() == "true"
    assert config["timestamp-rfc3161"].startswith("http")
    assert config["file-digest"] == "SHA256"
    assert int(config["timeout"]) >= 1200


def test_signed_wheels_are_verified_and_attested_before_they_are_uploaded(sign: dict) -> None:
    names = [s.get("name") for s in steps(sign, "sign")]
    order = ["Download unsigned wheels from the build run", "Unpack wheels for signing",
             "Sign native binaries with Azure Artifact Signing", "Repack signed wheels",
             "Verify signatures in the repacked wheels",
             "Attest the signed wheels and their evidence", "Upload signed wheels + evidence"]
    positions = [names.index(n) for n in order]
    assert positions == sorted(positions), names


@pytest.mark.parametrize("name", [SIGN, PUBLISH])
def test_no_job_holding_a_credential_installs_from_pypi(name: str) -> None:
    """A dependency installed beside the signing session, or the release and Kitmaker
    tokens, could rewrite the files before they are attested or released."""
    workflow = load(name)
    for job, spec in workflow["jobs"].items():
        if "inputs.runner-base" in str(spec.get("runs-on")):
            continue
        for step in steps(workflow, job):
            assert "pip install" not in str(step.get("run", "")), f"{name}:{job}:{step.get('name')}"


def test_package_metadata_is_checked_before_validation(sign: dict) -> None:
    names = [s.get("name") for s in steps(sign, "validate")]
    assert names.index("Download signed wheels") < names.index("Check package metadata") \
        < names.index("Validate signed wheels on WoA hardware")
    assert "twine check" in step_named(sign, "validate", "Check package metadata")["run"]


def test_signing_refuses_to_start_without_a_pinned_signer_subject(sign: dict) -> None:
    """Without it, both signature checks accept any valid signer, so a wrong
    certificate profile would go unnoticed."""
    gate = step_named(sign, "sign", "Require Azure Artifact Signing configuration")
    assert gate["env"]["WOA_EXPECTED_SIGNER_SUBJECT"] == "${{ vars.WOA_EXPECTED_SIGNER_SUBJECT }}"
    assert "'WOA_EXPECTED_SIGNER_SUBJECT'" in gate["run"]


def test_signing_attests_every_file_the_release_job_checks(sign: dict) -> None:
    """attestations.py refuses any wheel, manifest or signature report without one."""
    sys.path.insert(0, str(PUBLISH_SCRIPTS))
    import attestations

    (attest,) = uses(sign, "sign", "actions/attest@")
    assert attestations.SIGNER_WORKFLOW == f".github/workflows/{SIGN}"
    upload = step_named(sign, "sign", "Upload signed wheels + evidence")["with"]["path"]
    attested = [p.strip().replace("${{ inputs.python-label }}", "py313")
                for p in attest["with"]["subject-path"].splitlines() if p.strip()]
    assert all(p.startswith(f"{upload}/") for p in attested), attested
    for name in ("torch-2.14.0.dev20261003+cu134-cp313-cp313-win_arm64.whl",
                 "release-manifest-py313.json", "signature-report-py313.json"):
        assert attestations.SUBJECT.match(name)
        assert any(fnmatch.fnmatchcase(f"{upload}/{name}", p) for p in attested), name
    assert not attestations.SUBJECT.match("validation-py313.json")


def test_the_release_job_checks_attestations_before_releasing(publish: dict) -> None:
    names = [s.get("name") for s in steps(publish, "github-release")]
    check = "Check every signed file was attested by this run's signing job"
    assert names.index("Download signed wheels") < names.index(check)
    assert names.index("Download validation reports") < names.index(check)
    assert names.index(check) < names.index("Publish and verify the GitHub Release")
    script = squash(step_named(publish, "github-release", check)["run"].replace("\\\n", " "))
    assert "oot/scripts/publish/attestations.py --asset-dir assets" in script
    assert '--run-id "$GITHUB_RUN_ID"' in script


def test_signing_downloads_from_the_vetted_build_run(sign: dict) -> None:
    download = step_named(sign, "sign", "Download unsigned wheels from the build run")["with"]
    assert download["run-id"] == "${{ inputs.build-run-id }}"
    # By id: a replaced artifact has a new one, so the swap fails here instead of being signed.
    assert download["artifact-ids"] == "${{ inputs.unsigned-artifact-id }}"
    assert "name" not in download and str(download["merge-multiple"]).lower() == "true"
    repack = step_named(sign, "sign", "Repack signed wheels")["run"]
    assert '--build-run-id "${{ inputs.build-run-id }}"' in repack


def test_validation_reports_upload_even_on_failure(sign: dict) -> None:
    assert squash(step_named(sign, "validate", "Upload validation report")["if"]) == "${{ !cancelled() }}"


# --------------------------------------------------------------------------
# Names written by one piece and read by the next.
# --------------------------------------------------------------------------


def test_signing_reads_the_artifact_the_resolver_vetted(flow: dict) -> None:
    """build_run.py records the id of the artifact each cell's build job uploaded."""
    assert flow["jobs"]["sign"]["with"]["unsigned-artifact-id"] == "${{ matrix.config.artifact_id }}"


def test_the_resolver_recognises_the_build_artifacts_and_jobs(nightly: dict) -> None:
    """build_run.py finds cells by these names; if the build renames them, every cell
    silently becomes ineligible."""
    artifact = resolve(nightly["jobs"]["build"]["with"]["wheel-artifact"], run_id=BUILD_RUN_ID)
    match = build_run._ARTIFACT.match(artifact)
    assert match and match.groups() == ("py313", BUILD_RUN_ID)
    called_job = load(BUILD_CELL)["jobs"]["build"]["name"]
    job_name = f"{resolve(nightly['jobs']['build']['name'])} / {called_job}"
    assert build_run._BUILD_JOB.match(job_name), job_name


def test_the_resolver_knows_every_cell_the_build_matrix_has(nightly: dict) -> None:
    for cell in nightly["jobs"]["build"]["strategy"]["matrix"]["config"]:
        assert build_run.label_to_version(cell["label"]) == cell["version"]


@pytest.mark.parametrize("build_run_id, run_id", RUN_PAIRS)
def test_publish_downloads_exactly_what_signing_uploads(flow: dict, publish: dict, nightly: dict, build_run_id: str,
                                                        run_id: str) -> None:
    """Including on a nightly, where the unsigned build artifacts sit in the same run."""
    sign_with = flow["jobs"]["sign"]["with"]
    patterns = [resolve(s["with"]["pattern"], build_run_id=build_run_id, run_id=run_id)
                for s in uses(publish, "github-release", "actions/download-artifact@")]
    for key in ("signed-artifact", "validation-artifact"):
        name = resolve(sign_with[key], build_run_id=build_run_id, run_id=run_id)
        assert sum(fnmatch.fnmatchcase(name, p) for p in patterns) == 1, (key, name, patterns)
    # The build's `github.run_id` is the build run.
    unsigned = resolve(nightly["jobs"]["build"]["with"]["wheel-artifact"], run_id=build_run_id)
    assert not any(fnmatch.fnmatchcase(unsigned, p) for p in patterns)


def test_verify_reads_the_signed_manifest(flow: dict) -> None:
    jobs = flow["jobs"]
    assert jobs["verify"]["with"]["signed-artifact"] == jobs["sign"]["with"]["signed-artifact"]


def test_every_stage_uses_the_resolved_cells_and_build_run(flow: dict) -> None:
    jobs = flow["jobs"]
    assert jobs["resolve"]["steps"][-1]["env"]["BUILD_RUN_ID"] == "${{ inputs.build-run-id }}"
    for name in ("sign", "verify"):
        assert jobs[name]["strategy"]["matrix"]["config"] == "${{ fromJSON(needs.resolve.outputs.cells) }}"
    assert jobs["sign"]["with"]["build-run-id"] == "${{ needs.resolve.outputs.build-run-id }}"
    assert jobs["publish"]["with"]["build-run-id"] == "${{ needs.resolve.outputs.build-run-id }}"


@pytest.mark.parametrize("caller", CALLERS)
def test_callers_pass_every_publication_input_by_name(caller: str) -> None:
    """A misspelt `with:` key on a reusable workflow fails the run at startup."""
    declared = set(on(load(SIGN_PUBLISH))["workflow_call"]["inputs"])
    job = load(caller)["jobs"][CALLER_JOB[caller]]
    assert set(job["with"]) <= declared
    assert {"build-run-id", "channel"} <= set(job["with"])
    assert set(job["secrets"]) == {"KITMAKER_API_TOKEN"}


def test_release_report_name_is_shared_between_publication_jobs(publish: dict) -> None:
    uploaded = step_named(publish, "github-release", "Upload release report")["with"]["name"]
    for job in ("kitmaker-dry-run", "kitmaker-release"):
        names = [s["with"].get("name") for s in uses(publish, job, "actions/download-artifact@")]
        assert uploaded in names, job


def test_the_approved_dry_run_is_what_production_downloads(publish: dict) -> None:
    uploaded = step_named(publish, "kitmaker-dry-run", "Upload dry-run report")["with"]["name"]
    names = [s["with"].get("name") for s in uses(publish, "kitmaker-release", "actions/download-artifact@")]
    assert uploaded in names


# --------------------------------------------------------------------------
# Kitmaker ordering and transport.
# --------------------------------------------------------------------------


def test_production_requires_its_dry_run(publish: dict) -> None:
    jobs = publish["jobs"]
    assert jobs["kitmaker-release"]["needs"] == "kitmaker-dry-run"
    assert squash(jobs["kitmaker-release"]["if"]) == "inputs.run-kitmaker-release"
    gate = step_named(publish, "gate", "Validate the publication request")["run"]
    assert '"$RELEASE" == true && "$DRY_RUN" != true' in gate


def test_kitmaker_needs_its_point_of_contact_before_anything_is_released(publish: dict) -> None:
    gate = step_named(publish, "gate", "Validate the publication request")
    assert gate["env"]["KITMAKER_PIC"] == "${{ vars.KITMAKER_PIC }}"
    assert '"$DRY_RUN" == true && -z "${KITMAKER_PIC' in gate["run"]
    assert "gate" in publish["jobs"]["github-release"]["needs"]
    for job, step in [("kitmaker-dry-run", "Kitmaker dry run (upload=false)"),
                      ("kitmaker-release", "Kitmaker production release (upload=true)")]:
        assert step_named(publish, job, step)["env"]["KITMAKER_PIC"] == "${{ vars.KITMAKER_PIC }}"


def test_a_rehearsal_never_reaches_kitmaker(publish: dict) -> None:
    assert "inputs.channel != 'rehearsal'" in squash(publish["jobs"]["kitmaker-dry-run"]["if"])


def test_kitmaker_jobs_tunnel_through_charon(publish: dict) -> None:
    assert "ext-nv-prd-apps.teleport.sh:443" in publish["env"]["CHARON_TELEPORT_PROXY"]
    for job in ("kitmaker-probe", "kitmaker-dry-run", "kitmaker-release"):
        tunnel = uses(publish, job, "teleport-actions/application-tunnel@")
        assert len(tunnel) == 1, job
        assert tunnel[0]["with"]["token"] == "charon-gha-runners"
        assert tunnel[0]["with"]["app"] == "charon"
        assert tunnel[0]["with"]["listen"] == "tcp://127.0.0.1:8888"
        assert len(uses(publish, job, "teleport-actions/setup@")) == 1, job


@pytest.mark.parametrize("name", [*CALLERS, SIGN_PUBLISH, PR_PIPELINE, SIGN, PUBLISH, VERIFY])
def test_third_party_actions_are_pinned_to_a_commit(name: str) -> None:
    text = (WORKFLOWS / name).read_text(encoding="utf-8")
    for action in re.findall(r"^\s*(?:-\s+)?uses:\s*([^\s#]+)", text, flags=re.MULTILINE):
        if action.startswith("./"):
            continue
        assert re.search(r"@[0-9a-f]{40}$", action), f"{name}: {action} is not pinned to a full SHA"


# --------------------------------------------------------------------------
# The library the workflows drive.
# --------------------------------------------------------------------------


def test_workflows_call_scripts_that_exist() -> None:
    for name in (*CALLERS, SIGN_PUBLISH, SIGN, PUBLISH, VERIFY):
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        for script in re.findall(r"oot/((?:scripts|tools)/[\w./-]+\.(?:py|ps1))", text):
            assert (ROOT / script).is_file(), f"{name} calls missing {script}"
