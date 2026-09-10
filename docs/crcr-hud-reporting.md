<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: MIT
-->

# Reporting nightly results to the PyTorch HUD (CRCR L2)

How `windows-rtx-build-test.yml` and `windows-woa-build-test.yml` publish their
nightly results to the upstream PyTorch HUD through the Cross-Repo CI Relay
(CRCR), and what has to be true for those results to show up.

## Trust levels

CRCR gates downstream repositories by trust level in
[`pytorch/pytorch`'s `.github/allowlist.yml`](https://github.com/pytorch/pytorch/blob/main/.github/allowlist.yml):

| Level | What it grants |
| --- | --- |
| L1 | Upstream events are relayed *to* us. Nothing we send back is used. |
| L2 | Our results are accepted and rendered on the HUD (not on PRs). |

This repository is currently **L1**. Everything described here is implemented
and safe to run at L1 — it just has no visible effect yet, because the relay
accepts an L1 callback with `HTTP 200 {"ok": true, "status": "ignored"}` and
drops it. Promotion is a one-line move of the repo from the L1 list to the L2
list in the upstream allowlist. Note that the upstream parser raises on a repo
appearing under two levels, so it must be **moved**, not added.

## What gets reported

Both nightly workflows report. The names follow pytorch's own
`<build-environment> / <job> (<config>)` convention so they read natively
alongside upstream's rows:

**RTX — six rows**, one per matrix cell:

```
win-rtx-py312-cu130 / build          win-rtx-py312-cu132 / build
win-rtx-py312-cu130 / test (sm89)    win-rtx-py312-cu132 / test (sm89)
win-rtx-py312-cu130 / test (sm120)   win-rtx-py312-cu132 / test (sm120)
```

**WoA — two rows**, however many Python cells are enabled:

```
win-woa-arm64-cu134 / build
win-woa-arm64-cu134 / test (arm64)
```

Build rows carry a conclusion only; test rows also carry
passed/failed/skipped/total.

Upstream renders these as `crcr/<repo>/<workflow>/<job-name>`. The shard jobs
behind a test row — five on RTX, four per Python cell on WoA — are an
implementation detail of ours and are collapsed into the row rather than
reported individually: CRCR asks downstream repos to aggregate their internal
shards and avoid one callback per shard.

RTX keeps `cu130` and `cu132` on separate rows, so a regression that only
affects one CUDA version is attributable from the HUD rather than needing our
logs.

WoA goes the other way and reduces its whole Python axis into two rows. One row
per wheel per stage is more upstream surface than a small set of stable logical
jobs, and the trade is cheap for us: when a WoA row goes red, which Python
version broke is one click into our own run, where the shard jobs are still
named per version. The row name lives in the workflow-level `HUD_ENV`; the
per-cell `label` names GitHub jobs only and has to match what
`resolve_cell_conclusion.py` greps for.

### Where the two workflows differ

The WoA reporting jobs are otherwise a transcription of the RTX ones. Where
they diverge, it is forced by the WoA matrix:

1. **One row for the whole Python axis.** The reporting jobs are plain jobs
   rather than matrices. Each passes every enabled cell name to
   `resolve_cell_conclusion.py --cell` and takes the worst outcome, so one
   Python version failing to build cannot leave a green build row.
2. **Counts are summed per cell, not unioned.** `build_test_results.py
   --group-regex` splits the downloaded shards back out by Python label and
   scans each cell separately before summing. This matters because the cells
   all run the *same* test suite: unioning on test identity would collapse
   several runs of a test into one and understate the totals by roughly the
   number of cells. Failing tests are tagged with their cell (`py313:
   test_foo::test_bar`) so a test that fails on one version and passes on
   another stays attributable and is not cancelled out. It is a no-op while
   only one cell is enabled, and correct the moment a second one is.
3. **Expected shard count.** RTX derives it from the Jobs API (`matched-jobs`).
   WoA cannot: `_woa-test.yml` fans out through a `setup` job that GitHub names
   `<cell> / shard plan`, which carries the same prefix the shards do, so
   `matched-jobs` reads 5 for a healthy 4-shard cell and every green cell would
   report a phantom shortfall. WoA takes the count from `prep`, which is also
   what drives the fanout. Because one row spans several cells,
   `--expected-groups` carries what the shard tally cannot see: a whole Python
   cell that left no reports at all.
4. **Enabled-cell coupling.** WoA's matrix currently has all but one Python
   cell commented out. A commented-out matrix entry produces no job at all —
   not even a skipped one — so the workflow-level `PYTHON_LABELS` must list
   exactly the enabled cells. The two fail in opposite, both-safe directions: a
   label listed but not enabled fails the reporting job loudly, while a cell
   enabled but not listed is silently left out of its HUD row. Re-enabling a
   cell means editing both, in the same commit.
5. **Wheel date.** WoA stamps wheels `devYYYYMMDD` from a `nightly-date` taken
   from the nightly release commit rather than today's clock, so a re-run
   rebuilds the same version — the same reasoning as the SHA pin below. RTX has
   no equivalent input.

## The delivery ID is not the nightly branch tip

This is the part that is easy to get wrong. The HUD keys a result by
`delivery-id`, which it treats as a **`pytorch/pytorch` `main` commit** — it is
the correlation key that lines our row up against upstream's own results, and
it is linked as `github.com/pytorch/pytorch/commit/<sha>`.

`pytorch/pytorch`'s `nightly` branch is *generated*, not merged. Its commits
carry version mangling on top of `main` and do not exist on `main` at all, so
the branch tip is useless as a key. Each nightly commit does, however, name the
commit it was cut from:

```
2026-08-11 nightly release (f616cd499a809e339cbdd09901318bc52c06f86c)
```

`scripts/crcr/resolve_nightly_commits.py` extracts that embedded SHA, picking
the newest nightly at or before `--as-of` (see below) rather than the branch
tip. The workflow then **builds and tests that `main` commit** and reports
under it, so the thing we tested and the thing we report are the same commit.

### Re-runs must resolve the same SHA

Re-running a nightly is the sanctioned way to repair a day the HUD is missing,
so a re-run has to land on the row it is repairing. Nothing run-specific goes
into the key — `delivery-id` is the `main` SHA and `job-name` is the cell's HUD
name — so the same inputs overwrite the same row rather than adding a second
one.

The `main` SHA being *stable across attempts* takes explicit work, though,
because the `nightly` branch tip advances every night. Resolving "the tip" on a
re-run silently retargets a newer commit: the re-run builds something else and
files a row for a different day, leaving the gap it was meant to close.

`prep` therefore pins resolution with `--as-of`, passing attempt 1's
`run_started_at` — read from `…/actions/runs/<id>/attempts/1`, because the
run-level field resets on every re-run. Every attempt of a run then resolves
identically. The fetch window is 100 nightly commits so a run re-run weeks
later still finds its commit, and `resolve_nightly_commits.py` raises rather
than falling back to the oldest commit it happens to hold if the window falls
short.

The *evidence* a re-run reads stays scoped to its own attempt — the Jobs API
query and the artifact pattern both pin `run_attempt` — so a fresh attempt
recomputes its conclusion from its own results and never mixes in the previous
attempt's.

### Repair with "Re-run all jobs", not "Re-run failed jobs"

Attempt-scoped evidence is what makes a full re-run clean, and it is also what
makes a *partial* re-run wrong. "Re-run failed jobs" bumps `run_attempt` for
the whole run but re-executes only a subset, so shards that already passed keep
artifacts named for attempt 1 while the reporting job's pattern pins attempt 2.
Those shards read as missing, the shortfall check fires, and a cell that is
genuinely fine reports `failure`. The Jobs API side behaves the same way: if
`/attempts/<n>/jobs` omits carried-over jobs, `resolve_cell_conclusion.py`
raises for every untouched cell.

Widening the artifact pattern to ignore the attempt is not the fix — a full
re-run would then match both attempts' uploads for the same shard and resurrect
failures that have since been fixed. Use "Re-run all jobs".

## Which runs report

`prep` computes a `crcr-delivery-id` output, and every downstream reporting job
is switched on by it being non-empty. A run can never publish a row for a
commit that is not upstream `main`.

**WoA** has no manual trigger at all: the nightly schedule is its only caller,
so every run reports and there is nothing to opt out of.

**RTX** also accepts `workflow_dispatch`, so it carries the extra conditions:

- no `pytorch-pr` input (PR builds are not nightly results);
- `pytorch-ref` left at `nightly`, so the SHA came from the resolver above;
- the event is `schedule` or `workflow_dispatch`;
- `report-to-hud` is ticked.

**`report-to-hud` is off by default**, so a manual RTX run left at its defaults
publishes nothing even though `pytorch-ref` defaults to `nightly`. That is the
intended asymmetry: manual runs are usually matrix or runner experiments, and
the row for an upstream commit should be written by the schedule rather than
overwritten by a hand-started run. Tick it only when deliberately re-filing a
nightly row — and prefer **Re-run all jobs** on the original run for that, since
a re-run re-resolves the same SHA (see below) while a fresh manual run resolves
whatever nightly is current.

## Job shape

```
prep ──> build (matrix: config) ──> test (matrix: config [x arch]) ──> test-summary
  │        │                          │
  │        └──> report-build-crcr     └──> report-test-crcr
  └────────────────────────────────────────┘  (delivery-id)
```

Identical in both workflows; RTX's test matrix carries the extra `arch` axis.

Two properties are load-bearing:

**Reporting jobs are terminal.** Nothing depends on them, so a relay outage can
never skip a build or a test. This is why they are not steps inside the
reusable workflows: a callback failure inside `_rtx-build.yml` would fail the
caller's `build` job, which would skip the entire `test` matrix behind it. Real
CI coverage must not depend on a reporting side-channel.

**On RTX, reporting jobs are per-cell matrices with `fail-fast: false`**, so
each row is delivered independently and one failed callback cannot suppress the
others. WoA has only one row of each kind, so its reporting jobs are plain jobs
and the question does not arise.

Being terminal costs us `needs.<cell>.result`, which in the caller collapses to
a single aggregate across every matrix leg. The conclusion is therefore
recovered from the Jobs API via `scripts/crcr/resolve_cell_conclusion.py`,
matching the jobs named `"<cell> / ..."` — once per leg on RTX, once over every
enabled cell on WoA.

This reporting lives inside the two build/test workflows rather than in a
standalone `crcr-nightly.yml`. A separate workflow would have to re-run a
multi-hour Windows build matrix to have anything to report on. The isolation a
separate file would buy is already present at the job level: separate jobs,
separate runners, terminal, `fail-fast: false`.

## How a conclusion is decided

Conclusions fail closed — a wrong green row upstream is far more damaging than
a spurious red one.

A build cell reports its job conclusion directly. A test cell requires **two
independent signals to agree** before it is called green:

1. the shard jobs' own GitHub outcomes (worst wins; a real `failure` outranks a
   `cancelled` so an unrelated cancellation cannot hide a broken test);
2. the JUnit evidence the shards uploaded, aggregated by
   `scripts/crcr/build_test_results.py`.

Either signal alone has a blind spot: a shard cancelled after writing partial
XML looks clean to the summariser, and a harness that exits 0 while reporting
failed cases looks clean to GitHub. The summariser reports `failure` when fewer
shards left reports than `--expected-shards`, when any test is still failing
after rerun reconciliation, when a JUnit header declares more failures than it
itemises, or when a report could not be parsed at all — the last two both being
signatures of a process that died mid-write.

The unparsable-report check is deliberately belt-and-braces. `parse_failures`
already turns XML it cannot read into a synthetic `crash` failure, so such a
cell is red via the failing-test count alone; naming the counter in the
predicate too means a future change there that stopped synthesising that row
could not quietly turn corrupt evidence green.

The shard-count check is not redundant with signal 1. `upload-artifact` is
configured `if-no-files-found: warn`, so a shard that produced no reports tree
uploads nothing and still succeeds — its job stays green and only the short
count reveals that part of the cell never ran. On RTX the expected count is the
number of shard jobs the Jobs API listed for the cell, which signal 1 already
fetched, so changing `_rtx-test.yml`'s fanout needs no edit here.

Counting reuses `scripts/test-summary/parse_failures.py`, so the numbers sent
upstream are the same ones the run summary shows.

The HUD and GitHub conclusions also have to agree. Signal 2 can contradict
signal 1 — green shard jobs whose reports show failures — and in that case the
reporting job sends `failure` upstream and then deliberately exits 1, so the
run someone opens matches the row they saw on the HUD. The ordering matters:
the callback runs first, because a job that goes red before delivering its
result leaves the HUD with no row at all.

## Authentication

No long-lived credential. Each reporting job holds `id-token: write` and mints
a short-lived GitHub OIDC token for the `pytorch-cross-repo-ci-relay`
audience; the relay verifies it and reads the repository claim from the token
rather than trusting anything in the payload. The permission is granted per
job, so build and test runners never hold it.

## Why the callback action is cloned at run time

CRCR publishes a shared action,
`pytorch/test-infra/.github/actions/cross-repo-ci-relay-callback`, and using it
directly would be preferable. Every `uses:` in this repository resolves to
either `actions/*` or a local `./` path, though, because organisation policy
can restrict which third-party actions may run — and that restriction is
evaluated when the workflow file is *parsed*. A disallowed `uses:` anywhere in
the file makes the entire run a `startup_failure`, including jobs that would
never have executed, and guarding it behind an `if:` does not help.

`.github/actions/crcr-callback` is therefore a local wrapper, since local `./`
actions are exempt. It **clones `pytorch/test-infra` during the run** (sparse,
shallow) and executes upstream's `report_callback.py` from the clone, so no
part of the relay protocol — the payload shape, its schema version, the fields
the relay understands — is copied into this repository. An earlier revision did
vendor a byte-identical copy, and it drifted: upstream added
`failed_tests_detail` and this repository carried on sending the older payload
without any signal. The upstream commit that actually ran is printed into each
reporting job's summary, which is the one piece of provenance a floating ref
would otherwise lose.

`source-ref` defaults to `main` — tracking it is the point of cloning rather
than copying, and it is the ref upstream documents. Pin a SHA there if a run
ever needs to be reproducible against a known-good upstream, or if upstream
breaks us. The blast radius is small either way: these jobs run on ephemeral
`ubuntu-latest`, never on the self-hosted Windows pool, and the only credential
present is an OIDC token whose audience is the relay.

Two things are deliberately still ours rather than taken from upstream's
`action.yml`:

- **OIDC minting**, done with curl. Upstream uses `actions/github-script`,
  which would add a second action for policy to resolve at run time.
- **Retry around the callback.** Upstream retries the token mint but sends the
  callback exactly once, and a dropped callback means a missing row for the
  day. The retry is *classified*, not blind — see below.

Input names match upstream exactly, so if the shared action is ever permitted
here, switching to it is a one-line change per call site.

### Retry policy

Four attempts total, with exponential backoff and jitter (roughly 5s, 10s, 20s)
so that eight reporting jobs hitting one relay outage do not come back in
lockstep. What gets retried is decided by how the attempt failed, because the
relay should never receive the same rejected payload twice:

| Failure | Retried | Why |
| --- | --- | --- |
| DNS, connect, timeout, TLS, empty/partial transfer | yes | The request never landed. |
| HTTP 408, 429, or any 5xx | yes | The relay is asking to be tried later. |
| Any other 4xx (400, 401, 403, 404, 422, …) | **no** | The payload is wrong and will be just as wrong next time. |
| Payload validation errors raised before sending | **no** | Never reached the network. |

Upstream exits with curl's own status, which is what makes the class readable
without parsing prose; the HTTP code is only needed for the one status that
means "the relay answered and refused". If upstream ever rewords that line the
code cannot be read, and the attempt is treated as rejected — erring towards
not resending. Worst case per HUD row is four requests.

A callback that still fails is left to fail the job. Nothing depends on these
jobs, so a red reporting job blocks nothing, and it is the only signal that a
row is missing. Re-running resolves the same upstream SHA and refiles it.

### Checking upstream still fits

Tracking `main` means upstream can add a required input without telling us. The
wrapper supplies every variable upstream's script reads today; to confirm that
after an upstream change, clone the action and compare what it reads against
what the wrapper's `env:` block sets:

```bash
git clone --depth 1 --filter=blob:none --sparse \
  https://github.com/pytorch/test-infra /tmp/ti
git -C /tmp/ti sparse-checkout set .github/actions/cross-repo-ci-relay-callback
grep -oE "os\.environ(\[|\.get\()['\"][A-Z_]+" \
  /tmp/ti/.github/actions/cross-repo-ci-relay-callback/report_callback.py \
  | grep -oE "[A-Z_]+$" | sort -u
```

Anything in that list which the wrapper does not set is a break: a name read
via `os.environ[...]` raises, while one read via `os.environ.get(...)` is
optional and only means a field is being omitted from the payload.

## Before promotion to L2

1. Move this repository from the L1 list to the L2 list in the upstream
   allowlist, remembering it must not appear twice.
2. Confirm the nightly cron times still run late enough that the day's
   `nightly` commit already exists. If a schedule fires first, the resolver
   picks up the previous day's `main` SHA and files a stale row.
3. Expect eight rows a night once both workflows report: six from RTX and two
   from WoA.

## Local development

```bash
pytest scripts/crcr/tests -q
```

These run in CI via the `scripts` job in `lint.yml`.
