# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Tests for ``kitmaker.py``.

`kitmaker-release` is the one irreversible step in the pipeline. The cases that
matter are the ones that would publish something the dry run did not approve,
overwrite a file already on the index, or keep going after Charon stopped
recognising us - see Note [Production replays the dry run exactly] and Note
[The OIDC token is re-minted, not reused].
"""
from __future__ import annotations

import copy
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kitmaker as km  # noqa: E402

REPO = "NVIDIA/pytorch-windows-ci"
TAG = "woa-nightly-20260928-r42"
PREFIX = f"https://github.com/{REPO}/releases/download/{TAG}/"
VERSION = "2.14.0.dev20260928+cu134"


def wheel(package: str, cell: str) -> str:
    return f"{package}-{VERSION}-{cell}-{cell}-win_arm64.whl"


def release_report(cells=("cp313", "cp312"), **overrides) -> dict:
    assets = []
    for cell in cells:
        for package in ("torch", "torchaudio", "torchvision"):
            name = wheel(package, cell)
            assets.append({"name": name, "sha256": f"{package}-{cell}".ljust(64, "0")[:64],
                           "url": PREFIX + name.replace("+", "%2B")})
    assets.append({"name": "validation-py313.json", "sha256": "e" * 64, "url": PREFIX + "validation-py313.json"})
    report = {"repository": REPO, "tag": TAG, "channel": "nightly", "run_id": "42", "draft": False, "assets": assets}
    report.update(overrides)
    return report


class Response:
    def __init__(self, status: int, body):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def http_error(url: str, code: int, body=b"{}"):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body if isinstance(body, bytes) else json.dumps(body).encode()))


class StaticTokens:
    def __init__(self):
        self.calls = 0

    def get(self):
        self.calls += 1
        return f"oidc-{self.calls}"


class FakeKitmaker:
    """Charon + Kitmaker + pypi.nvidia.com behind one urlopen stand-in."""

    def __init__(self, *, statuses=("in_progress", "completed"), published=None, index_after=None):
        self.requests, self.submitted = [], []
        self._statuses = list(statuses)
        self.index = dict(published or {})
        self._index_after = index_after or {}

    def __call__(self, request, timeout=None):
        url = request.full_url
        self.requests.append(request)
        if url.startswith("https://pypi.nvidia.com/"):
            package = url.rstrip("/").rsplit("/", 1)[-1]
            links = [f'<a href="../../files/{n.replace("+", "%2B")}#sha256={h}">{n}</a>'
                     for n, h in self.index.items() if n.startswith(package + "-")]
            if not links:
                raise http_error(url, 404)
            return Response(200, "\n".join(links).encode())
        if request.get_method() == "POST":
            body = json.loads(request.data)
            self.submitted.append(body)
            return Response(202, {"release_uuid": f"uuid-{len(self.submitted)}"})
        status = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        if status == "completed" and self._index_after:
            self.index.update(self._index_after)
        return Response(200, {"status": status})


def portal(opener, tokens=None):
    return km.Portal("http://127.0.0.1:8888/kitmaker-portal/api/v0", "api-token", tokens or StaticTokens(),
                     opener=opener, sleep=lambda _: None)


# -- requests ----------------------------------------------------------------


def test_one_request_per_package_with_every_cell() -> None:
    requests = km.release_requests(release_report(), channel="nightly", pic="pic@nvidia.com")
    assert [(r.package, r.project_id) for r in requests] == [("torch", "3730"), ("torchaudio", "4391"), ("torchvision", "4390")]
    torch = requests[0].body
    assert torch["project_name"] == "torch"
    assert len(torch["payload"]) == 2
    for entry in torch["payload"]:
        assert entry == {"pic": "pic@nvidia.com", "job_type": "wheel-release-job", "url": entry["url"],
                         "upload": False, "devzone_subdir": "nvtorch_oot_nightly"}
        assert entry["url"].startswith(PREFIX)


def test_a_release_targets_the_stable_devzone() -> None:
    requests = km.release_requests(release_report(channel="release"), channel="release", pic="p")
    assert {e["devzone_subdir"] for r in requests for e in r.body["payload"]} == {"nvtorch_oot"}


@pytest.mark.parametrize(
    "report, channel, message",
    [
        (release_report(), "rehearsal", "cannot publish"),
        (release_report(), "release", "channel"),
        (release_report(draft=True), "nightly", "draft"),
    ],
)
def test_release_requests_refuse_the_wrong_context(report: dict, channel: str, message: str) -> None:
    with pytest.raises(km.KitmakerError, match=message):
        km.release_requests(report, channel=channel, pic="p")


def test_release_requests_refuse_a_url_outside_this_release() -> None:
    report = release_report()
    report["assets"][0]["url"] = "https://github.com/someone/else/releases/download/x/" + report["assets"][0]["name"]
    with pytest.raises(km.KitmakerError, match="not under"):
        km.release_requests(report, channel="nightly", pic="p")


def test_release_requests_refuse_a_url_for_another_file() -> None:
    report = release_report()
    report["assets"][0]["url"] = PREFIX + report["assets"][1]["name"]
    with pytest.raises(km.KitmakerError, match="its own filename"):
        km.release_requests(report, channel="nightly", pic="p")


def test_release_requests_need_every_package() -> None:
    report = release_report()
    report["assets"] = [a for a in report["assets"] if not a["name"].startswith("torchaudio-")]
    with pytest.raises(km.KitmakerError, match="expected wheels"):
        km.release_requests(report, channel="nightly", pic="p")


def test_one_stable_abi_wheel_serves_every_cell() -> None:
    report = release_report()
    abi3 = f"torchaudio-{VERSION}-cp310-abi3-win_arm64.whl"
    report["assets"] = [a for a in report["assets"] if not a["name"].startswith("torchaudio-")]
    report["assets"].append({"name": abi3, "sha256": "a" * 64, "url": PREFIX + abi3.replace("+", "%2B")})
    requests = {r.package: r.body["payload"] for r in km.release_requests(report, channel="nightly", pic="p")}
    assert [len(requests[p]) for p in ("torch", "torchaudio", "torchvision")] == [2, 1, 2]


def test_production_only_flips_upload() -> None:
    body = km.release_requests(release_report(), channel="nightly", pic="p")[0].body
    production = km.production_body(body)
    assert all(e["upload"] is True for e in production["payload"])
    flipped_back = copy.deepcopy(production)
    for entry in flipped_back["payload"]:
        entry["upload"] = False
    assert flipped_back == body


def test_production_refuses_a_body_that_was_not_a_dry_run() -> None:
    body = km.release_requests(release_report(), channel="nightly", pic="p")[0].body
    body["payload"][0]["upload"] = True
    with pytest.raises(km.KitmakerError, match="not upload=false"):
        km.production_body(body)


# -- tokens and transport ----------------------------------------------------


def test_oidc_tokens_are_minted_for_charon_and_re_minted_before_expiry(capsys) -> None:
    now = [0.0]
    seen = []

    def opener(request, timeout=None):
        seen.append((request.full_url, request.headers["Authorization"]))
        return Response(200, {"value": f"jwt-{len(seen)}"})

    tokens = km.OidcTokens({"ACTIONS_ID_TOKEN_REQUEST_URL": "https://t/x?api-version=2",
                            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "req"}, opener=opener, clock=lambda: now[0])
    assert tokens.get() == "jwt-1"
    now[0] = 200
    assert tokens.get() == "jwt-1"
    now[0] = 241
    assert tokens.get() == "jwt-2"
    assert seen[0] == ("https://t/x?api-version=2&audience=charon.nvidia.com", "bearer req")
    assert "::add-mask::jwt-1" in capsys.readouterr().out


def test_oidc_tokens_need_the_id_token_permission() -> None:
    with pytest.raises(km.KitmakerError, match="id-token: write"):
        km.OidcTokens({}).get()


def test_submit_sends_both_credentials_and_returns_the_uuid() -> None:
    fake = FakeKitmaker()
    result = portal(fake).submit("3730", {"project_name": "torch", "payload": []})
    assert result["release_uuid"] == "uuid-1"
    request = fake.requests[0]
    assert request.full_url.endswith("/kitmaker-portal/api/v0/projects/3730/releases")
    assert request.headers["Authorization"] == "Bearer api-token"
    assert request.headers["X-charon-gha-token"] == "oidc-1"


@pytest.mark.parametrize("code, body", [(403, {"error": "denied"}), (202, {"no": "uuid"})])
def test_submit_refuses_a_bad_response(code: int, body: dict) -> None:
    def opener(request, timeout=None):
        if code >= 400:
            raise http_error(request.full_url, code, body)
        return Response(code, body)

    with pytest.raises(km.KitmakerError):
        portal(opener).submit("3730", {})


def test_wait_polls_through_transient_errors_to_completion() -> None:
    replies = [http_error("u", 503), Response(200, {"status": "processing"}), Response(200, {"status": "completed"})]

    def opener(request, timeout=None):
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    tokens = StaticTokens()
    assert portal(opener, tokens).wait("u", poll_seconds=1, timeout_seconds=60)["status"] == "completed"
    assert tokens.calls == 3  # a token per request, so a long poll never outlives one


@pytest.mark.parametrize("reply, message", [(Response(200, {"status": "failed"}), "ended failed"),
                                            (http_error("u", 403), "HTTP 403")])
def test_wait_stops_on_a_terminal_answer(reply, message: str) -> None:
    def opener(request, timeout=None):
        if isinstance(reply, Exception):
            raise reply
        return reply

    with pytest.raises(km.KitmakerError, match=message):
        portal(opener).wait("u", poll_seconds=1, timeout_seconds=60)


def test_wait_times_out() -> None:
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    p = km.Portal("http://x", "t", StaticTokens(), opener=lambda r, timeout=None: Response(200, {"status": "queued"}),
                  sleep=sleep, clock=lambda: now[0])
    with pytest.raises(km.KitmakerError, match="timed out"):
        p.wait("u", poll_seconds=30, timeout_seconds=90)


# -- index -------------------------------------------------------------------


def test_published_digest_reads_encoded_hrefs() -> None:
    name = wheel("torch", "cp313")
    fake = FakeKitmaker(published={name: "a" * 64})
    assert km.published_digest(km.INDEXES["nightly"], "torch", name, opener=fake) == "a" * 64
    assert km.published_digest(km.INDEXES["nightly"], "torch", wheel("torch", "cp312"), opener=fake) is None
    assert km.published_digest(km.INDEXES["nightly"], "torchvision", name, opener=fake) is None


def test_published_digest_refuses_ambiguity() -> None:
    name = wheel("torch", "cp313")
    html = f'<a href="{name}#sha256={"a" * 64}"></a><a href="{name}#sha256={"b" * 64}"></a>'.encode()
    with pytest.raises(km.KitmakerError, match="more than one"):
        km.published_digest("https://pypi.nvidia.com/x", "torch", name, opener=lambda r, timeout=None: Response(200, html))


# -- commands ----------------------------------------------------------------


def all_published(report: dict) -> dict[str, str]:
    return {a["name"]: a["sha256"] for a in report["assets"] if a["name"].endswith(".whl")}


def test_dry_run_submits_upload_false_and_records_the_bodies() -> None:
    fake = FakeKitmaker()
    result = km.dry_run(release_report(), channel="nightly", pic="p", portal=portal(fake), poll=1, timeout=60, opener=fake)
    assert [s["payload"][0]["upload"] for s in fake.submitted] == [False, False, False]
    assert [r["package"] for r in result["requests"]] == ["torch", "torchaudio", "torchvision"]
    assert set(result["index_state_before"].values()) == {"absent"}


def test_dry_run_refuses_to_overwrite_a_published_filename() -> None:
    report = release_report()
    name = wheel("torch", "cp313")
    fake = FakeKitmaker(published={name: "f" * 64})
    with pytest.raises(km.KitmakerError, match="different SHA-256"):
        km.dry_run(report, channel="nightly", pic="p", portal=portal(fake), poll=1, timeout=60, opener=fake)
    assert fake.submitted == []


def approved_dry_run(report: dict) -> dict:
    fake = FakeKitmaker()
    return km.dry_run(report, channel="nightly", pic="p", portal=portal(fake), poll=1, timeout=60, opener=fake)


def run_release(report: dict, approved: dict, fake: FakeKitmaker):
    return km.release(report, approved, channel="nightly", pic="p", portal=portal(fake), poll=1, timeout=60,
                      index_poll=1, index_timeout=60, opener=fake, sleep=lambda _: None)


def test_release_replays_the_dry_run_and_waits_for_the_index() -> None:
    report = release_report()
    approved = approved_dry_run(report)
    fake = FakeKitmaker(index_after=all_published(report))
    result = run_release(report, approved, fake)
    assert [s["payload"][0]["upload"] for s in fake.submitted] == [True, True, True]
    for submitted, dry in zip(fake.submitted, approved["requests"]):
        assert km.production_body(dry["body"]) == submitted
    assert set(result["index_state_after"].values()) == {"present"}


def test_release_refuses_a_payload_the_dry_run_did_not_see() -> None:
    report = release_report()
    approved = approved_dry_run(report)
    approved["requests"][0]["body"]["payload"][0]["devzone_subdir"] = "nvtorch_oot"
    fake = FakeKitmaker()
    with pytest.raises(km.KitmakerError, match="differs from this release"):
        run_release(report, approved, fake)
    assert fake.submitted == []


def test_release_refuses_an_approval_from_another_run() -> None:
    report = release_report()
    approved = approved_dry_run(report)
    approved["run_id"] = "41"
    with pytest.raises(km.KitmakerError, match="run_id"):
        run_release(report, approved, FakeKitmaker())


def test_release_refuses_an_incomplete_dry_run() -> None:
    report = release_report()
    approved = approved_dry_run(report)
    approved["requests"][1]["final_status"] = {"status": "failed"}
    with pytest.raises(km.KitmakerError, match="did not complete"):
        run_release(report, approved, FakeKitmaker())


def test_release_is_a_no_op_when_everything_is_already_published() -> None:
    report = release_report()
    approved = approved_dry_run(report)
    fake = FakeKitmaker(published=all_published(report))
    result = run_release(report, approved, fake)
    assert fake.submitted == [] and result["requests"] == []


def test_release_refuses_a_partial_publication() -> None:
    report = release_report()
    approved = approved_dry_run(report)
    name = wheel("torch", "cp313")
    fake = FakeKitmaker(published={name: all_published(report)[name]})
    with pytest.raises(km.KitmakerError, match="partial publication"):
        run_release(report, approved, fake)
    assert fake.submitted == []


def test_release_fails_if_the_index_never_catches_up() -> None:
    report = release_report()
    approved = approved_dry_run(report)
    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    fake = FakeKitmaker()
    with pytest.raises(km.KitmakerError, match="still does not list"):
        km.release(report, approved, channel="nightly", pic="p", portal=portal(fake), poll=1, timeout=60,
                   index_poll=30, index_timeout=90, opener=fake, sleep=sleep, clock=lambda: now[0])


@pytest.mark.parametrize("event", sorted(km.REFUSED_EVENTS))
def test_pr_events_are_refused(event: str) -> None:
    with pytest.raises(km.KitmakerError, match="never PR-triggered"):
        km.refuse_untrusted_event({"GITHUB_EVENT_NAME": event})


def test_trusted_events_pass() -> None:
    km.refuse_untrusted_event({"GITHUB_EVENT_NAME": "schedule"})
    km.refuse_untrusted_event({"GITHUB_EVENT_NAME": "workflow_dispatch"})


@pytest.mark.parametrize("code, reached", [(404, True), (200, True), (403, False), (401, False)])
def test_probe_distinguishes_kitmaker_from_charon(code: int, reached: bool) -> None:
    def opener(request, timeout=None):
        assert request.full_url.endswith(f"/status/{km.NIL_UUID}")
        if code >= 400:
            raise http_error(request.full_url, code)
        return Response(code, {"status": "unknown"})

    assert km.probe(portal(opener))["reached_kitmaker"] is reached


@pytest.mark.parametrize("command", ["dry-run", "release"])
def test_no_request_goes_out_without_a_point_of_contact(command: str, tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("KITMAKER_PIC", raising=False)
    monkeypatch.setenv("KITMAKER_API_TOKEN", "token")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    args = [command, "--release-report", str(tmp_path / "missing.json"), "--channel", "nightly",
            "--report", str(tmp_path / "out.json")]
    if command == "release":
        args += ["--dry-run-report", str(tmp_path / "missing-dry-run.json")]
    assert km.main(args) == 1
    assert "KITMAKER_PIC" in capsys.readouterr().err
    assert not (tmp_path / "out.json").exists()
