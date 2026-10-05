#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT
"""Release the WoA wheels from a GitHub Release to pypi.nvidia.com through Kitmaker.

Kitmaker's Portal API is not reachable from GitHub-hosted runners directly. It is
reached through Charon Ferry: a Teleport application tunnel (opened by the
workflow with teleport-actions) on 127.0.0.1:8888, plus a per-request GitHub OIDC
token in `X-Charon-GHA-Token` that Charon checks against this repository's tenant
grant. Kitmaker itself pulls the wheels from the public release URLs, so only the
release request goes through the tunnel.

Note [The OIDC token is re-minted, not reused]
    GitHub OIDC tokens live for about five minutes, and a Kitmaker poll can run
    for thirty. A token minted once at job start expires mid-poll and Charon
    starts answering 401. Tokens are therefore minted on demand and replaced
    after four minutes.

Note [Production replays the dry run exactly]
    The production request is the dry-run request with `upload` flipped to true
    and nothing else. `release` rebuilds the dry-run bodies from the current
    release report and requires them to equal the ones the dry run actually
    submitted, then derives production from those. So production cannot publish
    a wheel URL, devzone or package mapping that the dry run did not validate.

Note [The index is checked before and after]
    Before submitting, each wheel's filename is looked up on the destination
    index. The same filename with a different SHA-256 fails immediately: that
    would be an overwrite, which Kitmaker should refuse anyway, but the failure
    here names the file. After production completes, the index is polled until
    every filename is listed with the expected SHA-256, which is the "final
    remote hash" the release evidence needs.

Note [Pull requests never reach this]
    The orchestrator's event gate already keeps pull-request and relayed-PR runs
    away from publication. This script refuses those events again, so that a
    future workflow edit which loses the gate still cannot hand a Kitmaker
    token to PR-triggered code.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

PROJECTS = {"torch": "3730", "torchvision": "4390", "torchaudio": "4391"}
DEVZONES = {"nightly": "nvtorch_oot_nightly", "release": "nvtorch_oot"}
INDEXES = {
    "nightly": "https://pypi.nvidia.com/nvtorch_oot_nightly",
    "release": "https://pypi.nvidia.com/nvtorch_oot",
}
DEFAULT_PORTAL = "http://127.0.0.1:8888/kitmaker-portal/api/v0"
CHARON_AUDIENCE = "charon.nvidia.com"
JOB_TYPE = "wheel-release-job"
TOKEN_MAX_AGE_SECONDS = 240
TRUSTED_EVENTS = {"schedule", "workflow_dispatch"}
FAILED_STATUSES = {"failed", "error", "cancelled", "canceled", "rejected"}
NIL_UUID = "00000000-0000-0000-0000-000000000000"

_WHEEL = re.compile(r"^(torch|torchaudio|torchvision)-[^-]+-[^-]+-[^-]+-win_arm64\.whl$")

Opener = Callable[..., object]


class KitmakerError(RuntimeError):
    pass


def refuse_untrusted_event(env: dict[str, str]) -> None:
    event = env.get("GITHUB_EVENT_NAME", "")
    if event not in TRUSTED_EVENTS:
        raise KitmakerError(f"refusing to run for a {event!r} event; only {sorted(TRUSTED_EVENTS)} runs publish")


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseRequest:
    package: str
    project_id: str
    body: dict


def release_requests(report: dict, *, channel: str, pic: str) -> list[ReleaseRequest]:
    """One upload=false request per package, each listing that package's wheel URLs."""
    if channel not in DEVZONES:
        raise KitmakerError(f"channel {channel!r} cannot publish through Kitmaker")
    if report.get("channel") != channel:
        raise KitmakerError(f"release report is for channel {report.get('channel')!r}, not {channel!r}")
    if report.get("draft"):
        raise KitmakerError("a draft release has no public asset URLs for Kitmaker to fetch")
    prefix = f"https://github.com/{report['repository']}/releases/download/{report['tag']}/"
    urls: dict[str, list[str]] = {}
    for asset in report.get("assets", []):
        name = asset["name"]
        match = _WHEEL.match(name)
        if not match:
            continue
        url = asset.get("url") or ""
        if not url.startswith(prefix):
            raise KitmakerError(f"{name} URL {url!r} is not under {prefix}")
        if urllib.parse.unquote(url[len(prefix):]) != name:
            raise KitmakerError(f"{name} URL {url!r} does not end in its own filename")
        urls.setdefault(match.group(1), []).append(url)
    if set(urls) != set(PROJECTS):
        raise KitmakerError(f"expected wheels for {sorted(PROJECTS)}, found {sorted(urls)}")
    return [
        ReleaseRequest(
            package=package,
            project_id=PROJECTS[package],
            body={
                "project_name": package,
                "payload": [
                    {
                        "pic": pic,
                        "job_type": JOB_TYPE,
                        "url": url,
                        "upload": False,
                        "devzone_subdir": DEVZONES[channel],
                    }
                    for url in sorted(urls[package])
                ],
            },
        )
        for package in sorted(urls)
    ]


def production_body(dry_run_body: dict) -> dict:
    """The dry-run body with `upload` flipped - see Note [Production replays the dry run exactly]."""
    body = copy.deepcopy(dry_run_body)
    for entry in body["payload"]:
        if entry.get("upload") is not False:
            raise KitmakerError("the approved dry-run body is not upload=false")
        entry["upload"] = True
    check = copy.deepcopy(body)
    for entry in check["payload"]:
        entry["upload"] = False
    if check != dry_run_body:
        raise KitmakerError("production body differs from the dry run in more than `upload`")
    return body


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class OidcTokens:
    """GitHub OIDC tokens for Charon, re-minted before they expire."""

    def __init__(
        self,
        env: dict[str, str],
        *,
        opener: Opener = urllib.request.urlopen,
        clock: Callable[[], float] = time.monotonic,
        audience: str = CHARON_AUDIENCE,
        max_age: float = TOKEN_MAX_AGE_SECONDS,
    ):
        self._env, self._open, self._clock = env, opener, clock
        self._audience, self._max_age = audience, max_age
        self._token, self._minted = "", 0.0

    def get(self) -> str:
        if self._token and self._clock() - self._minted < self._max_age:
            return self._token
        url = self._env.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
        bearer = self._env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
        if not url or not bearer:
            raise KitmakerError("no GitHub OIDC request credentials; the job needs `permissions: id-token: write`")
        separator = "&" if "?" in url else "?"
        request = urllib.request.Request(
            f"{url}{separator}audience={urllib.parse.quote(self._audience)}",
            headers={"Authorization": f"bearer {bearer}"},
        )
        with self._open(request, timeout=60) as response:
            value = json.loads(response.read().decode("utf-8")).get("value", "")
        if not value:
            raise KitmakerError("GitHub returned an empty OIDC token")
        print(f"::add-mask::{value}")
        self._token, self._minted = value, self._clock()
        return value


def _decode(raw: bytes):
    text = raw.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


class Portal:
    """Kitmaker Portal calls through the Charon tunnel."""

    def __init__(
        self,
        base: str,
        api_token: str,
        tokens: OidcTokens,
        *,
        opener: Opener = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not api_token:
            raise KitmakerError("KITMAKER_API_TOKEN is not set")
        self._base, self._api_token, self._tokens = base.rstrip("/"), api_token, tokens
        self._open, self._sleep, self._clock = opener, sleep, clock

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        headers = {
            "Authorization": f"Bearer {self._api_token}",
            "X-Charon-GHA-Token": self._tokens.get(),
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self._base}/{path.lstrip('/')}", data=data, method=method, headers=headers)
        try:
            with self._open(request, timeout=120) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as err:
            return err.code, _decode(err.read())

    def submit(self, project_id: str, body: dict) -> dict:
        code, payload = self.request("POST", f"projects/{project_id}/releases", body)
        if code not in (200, 201, 202):
            raise KitmakerError(f"release submission for project {project_id} returned HTTP {code}: {payload}")
        uuid = payload.get("release_uuid") if isinstance(payload, dict) else None
        if not isinstance(uuid, str) or not uuid:
            raise KitmakerError(f"release submission for project {project_id} returned no release_uuid: {payload}")
        return payload

    def wait(self, uuid: str, *, poll_seconds: float, timeout_seconds: float) -> dict:
        """Poll to `completed`. Transient transport errors and 429/5xx are retried until the deadline."""
        deadline = self._clock() + timeout_seconds
        while True:
            try:
                code, payload = self.request("GET", f"status/{uuid}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
                code, payload = None, str(err)
            if code == 200 and isinstance(payload, dict):
                status = str(payload.get("status", "")).lower()
                print(f"kitmaker {uuid}: {status or '<no status>'}")
                if status == "completed":
                    return payload
                if status in FAILED_STATUSES:
                    raise KitmakerError(f"Kitmaker release {uuid} ended {status}: {json.dumps(payload)}")
            elif code is not None and code != 429 and code < 500:
                raise KitmakerError(f"status poll for {uuid} returned HTTP {code}: {payload}")
            if self._clock() >= deadline:
                raise KitmakerError(f"timed out after {timeout_seconds}s waiting for Kitmaker release {uuid}")
            self._sleep(poll_seconds)


# ---------------------------------------------------------------------------
# Destination index
# ---------------------------------------------------------------------------


def published_digest(index_url: str, package: str, filename: str, *, opener: Opener = urllib.request.urlopen) -> str | None:
    """The SHA-256 the index lists for `filename`, or None if it is not listed."""
    request = urllib.request.Request(
        f"{index_url.rstrip('/')}/{package}/",
        headers={"Accept": "text/html", "Cache-Control": "no-cache"},
    )
    try:
        with opener(request, timeout=60) as response:
            html = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return None
        raise
    digests = set()
    for href in re.findall(r'href="([^"]+)"', html):
        path, _, fragment = href.partition("#")
        if urllib.parse.unquote(path.rsplit("/", 1)[-1]) == filename and fragment.startswith("sha256="):
            digests.add(fragment[len("sha256="):].lower())
    if len(digests) > 1:
        raise KitmakerError(f"{index_url} lists {filename} with more than one SHA-256")
    return next(iter(digests), None)


def index_state(report: dict, index_url: str, *, opener: Opener = urllib.request.urlopen) -> dict[str, str]:
    """`absent`, `present`, or `mismatch` for every wheel in the release report."""
    state = {}
    for asset in report["assets"]:
        match = _WHEEL.match(asset["name"])
        if not match:
            continue
        digest = published_digest(index_url, match.group(1), asset["name"], opener=opener)
        if digest is None:
            state[asset["name"]] = "absent"
        elif digest == asset["sha256"]:
            state[asset["name"]] = "present"
        else:
            state[asset["name"]] = "mismatch"
    return state


def _refuse_overwrite(state: dict[str, str], index_url: str) -> None:
    mismatched = sorted(n for n, s in state.items() if s == "mismatch")
    if mismatched:
        raise KitmakerError(f"{index_url} already lists these filenames with a different SHA-256: {mismatched}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _run_requests(portal: Portal, bodies: list[tuple[ReleaseRequest, dict]], *, poll: float, timeout: float) -> list[dict]:
    results = []
    for request, body in bodies:
        upload = body["payload"][0]["upload"]
        print(f"submitting {request.package} (project {request.project_id}, {len(body['payload'])} wheel(s), upload={upload})")
        submission = portal.submit(request.project_id, body)
        final = portal.wait(submission["release_uuid"], poll_seconds=poll, timeout_seconds=timeout)
        results.append(
            {
                "package": request.package,
                "project_id": request.project_id,
                "body": body,
                "release_uuid": submission["release_uuid"],
                "submission": submission,
                "final_status": final,
            }
        )
    return results


def dry_run(report: dict, *, channel: str, pic: str, portal: Portal, poll: float, timeout: float, opener: Opener = urllib.request.urlopen) -> dict:
    requests = release_requests(report, channel=channel, pic=pic)
    state = index_state(report, INDEXES[channel], opener=opener)
    _refuse_overwrite(state, INDEXES[channel])
    results = _run_requests(portal, [(r, r.body) for r in requests], poll=poll, timeout=timeout)
    return {
        "schema_version": 1,
        "mode": "dry-run",
        "channel": channel,
        "repository": report["repository"],
        "tag": report["tag"],
        "run_id": report["run_id"],
        "index": INDEXES[channel],
        "index_state_before": state,
        "requests": results,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def approval(dry_run_result: dict) -> dict:
    """The part of a dry-run result `release` checks, small enough to hand on as a job output."""
    keys = ("schema_version", "mode", "channel", "repository", "tag", "run_id")
    return {
        **{key: dry_run_result[key] for key in keys},
        "requests": [
            {"package": r["package"], "project_id": r["project_id"], "body": r["body"],
             "release_uuid": r["release_uuid"], "final_status": {"status": r["final_status"].get("status")}}
            for r in dry_run_result["requests"]
        ],
    }


def release(
    report: dict,
    approved: dict,
    *,
    channel: str,
    pic: str,
    portal: Portal,
    poll: float,
    timeout: float,
    index_poll: float,
    index_timeout: float,
    opener: Opener = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    for key in ("channel", "repository", "tag", "run_id"):
        if approved.get(key) != (channel if key == "channel" else report.get(key)):
            raise KitmakerError(f"dry-run report {key} {approved.get(key)!r} does not match this release")
    if approved.get("mode") != "dry-run":
        raise KitmakerError("the approval report is not a dry run")

    # See Note [Production replays the dry run exactly].
    expected = release_requests(report, channel=channel, pic=pic)
    submitted = {r["package"]: r for r in approved.get("requests", [])}
    if set(submitted) != {r.package for r in expected}:
        raise KitmakerError(f"dry run covered {sorted(submitted)}, the release needs {sorted(r.package for r in expected)}")
    bodies = []
    for request in expected:
        done = submitted[request.package]
        if str(done.get("final_status", {}).get("status", "")).lower() != "completed":
            raise KitmakerError(f"the dry run for {request.package} did not complete")
        if done.get("body") != request.body or done.get("project_id") != request.project_id:
            raise KitmakerError(f"the dry-run request for {request.package} differs from this release")
        bodies.append((request, production_body(done["body"])))

    index_url = INDEXES[channel]
    before = index_state(report, index_url, opener=opener)
    _refuse_overwrite(before, index_url)
    if before and all(s == "present" for s in before.values()):
        print("every wheel is already on the index with the expected SHA-256; nothing to submit")
        results = []
    elif any(s == "present" for s in before.values()):
        present = sorted(n for n, s in before.items() if s == "present")
        raise KitmakerError(f"partial publication already on {index_url}: {present}; resolve manually")
    else:
        results = _run_requests(portal, bodies, poll=poll, timeout=timeout)

    # See Note [The index is checked before and after].
    deadline = clock() + index_timeout
    while True:
        after = index_state(report, index_url, opener=opener)
        _refuse_overwrite(after, index_url)
        if all(s == "present" for s in after.values()):
            break
        if clock() >= deadline:
            missing = sorted(n for n, s in after.items() if s != "present")
            raise KitmakerError(f"Kitmaker completed but {index_url} still does not list: {missing}")
        sleep(index_poll)

    return {
        "schema_version": 1,
        "mode": "production",
        "channel": channel,
        "repository": report["repository"],
        "tag": report["tag"],
        "run_id": report["run_id"],
        "index": index_url,
        "index_state_before": before,
        "index_state_after": after,
        "requests": results,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def probe(portal: Portal) -> dict:
    """Reach Kitmaker through Charon without submitting anything.

    Asks for the status of the nil UUID. Kitmaker answering (404 or 200) proves
    the tunnel, the Charon tenant grant, and the Kitmaker token all work. The
    status endpoint also answers anonymous callers, so this relies on Kitmaker
    refusing a token that does not authenticate with 401 instead of falling back
    to the anonymous answer.
    """
    code, payload = portal.request("GET", f"status/{NIL_UUID}")
    reached = code in (200, 404)
    return {"http_status": code, "reached_kitmaker": reached, "body": payload}


PROBE_HINTS = {
    401: "OIDC token rejected by Charon (tunnel or audience), or Kitmaker rejected KITMAKER_API_TOKEN",
    403: "Charon denied the request: check the gha-tenants grant's repository, allowed_refs and routes",
    502: "Charon could not reach Kitmaker",
    503: "Charon could not reach Kitmaker",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--portal-url", default=DEFAULT_PORTAL)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="reach Kitmaker through Charon without submitting anything")

    for name in ("dry-run", "release"):
        p = sub.add_parser(name)
        p.add_argument("--release-report", type=Path, required=True)
        p.add_argument("--channel", choices=sorted(DEVZONES), required=True)
        p.add_argument("--report", type=Path, required=True)
        p.add_argument("--pic", default=os.environ.get("KITMAKER_PIC", ""))
        p.add_argument("--poll-seconds", type=float, default=30)
        p.add_argument("--timeout-seconds", type=float, default=1800)
        if name == "dry-run":
            p.add_argument("--github-output", type=Path, help="append the approval for the release job as `approved`")
        if name == "release":
            p.add_argument("--dry-run-report", type=Path, required=True)
            p.add_argument("--index-poll-seconds", type=float, default=30)
            p.add_argument("--index-timeout-seconds", type=float, default=1200)

    args = parser.parse_args(argv)
    env = dict(os.environ)
    try:
        refuse_untrusted_event(env)
        api_token = env.get("KITMAKER_API_TOKEN", "")
        if api_token:
            print(f"::add-mask::{api_token}")
        portal = Portal(args.portal_url, api_token, OidcTokens(env))

        if args.command == "probe":
            result = probe(portal)
            print(json.dumps(result, indent=2))
            if not result["reached_kitmaker"]:
                hint = PROBE_HINTS.get(result["http_status"], "unexpected response")
                raise KitmakerError(f"probe got HTTP {result['http_status']}: {hint}")
            return 0

        if not args.pic.strip():
            raise KitmakerError("no point of contact: set the KITMAKER_PIC repository variable "
                                "(see docs/woa-wheel-publishing.md)")
        report = json.loads(args.release_report.read_text(encoding="utf-8"))
        repository = env.get("GITHUB_REPOSITORY")
        if repository and report.get("repository") != repository:
            raise KitmakerError(f"release report is for {report.get('repository')}, this run is {repository}")
        if args.command == "dry-run":
            result = dry_run(report, channel=args.channel, pic=args.pic, portal=portal,
                             poll=args.poll_seconds, timeout=args.timeout_seconds)
        else:
            approved = json.loads(args.dry_run_report.read_text(encoding="utf-8"))
            result = release(report, approved, channel=args.channel, pic=args.pic, portal=portal,
                             poll=args.poll_seconds, timeout=args.timeout_seconds,
                             index_poll=args.index_poll_seconds, index_timeout=args.index_timeout_seconds)
    except (KitmakerError, OSError, ValueError, KeyError) as err:
        print(f"::error title=kitmaker::{err}", file=sys.stderr)
        return 1

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    if args.command == "dry-run" and args.github_output:
        with args.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"approved={json.dumps(approval(result), separators=(',', ':'))}\n")
    print(f"kitmaker {result['mode']} completed for {result['tag']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
