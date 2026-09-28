<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: MIT
-->

# Per-PR CI: the allowlist and maintainer approval

**Audience:** maintainers of this repository who decide which `pytorch/pytorch`
pull requests get Windows CI.

The upstream Relay Server forwards every `pytorch/pytorch` PR event to this
repository — on the order of one every few minutes. `upstream-pull.yml` decides
which of those are worth a maintainer's attention, asks for approval, and then
runs both Windows pipelines against the commit that was approved.

It builds nothing itself. It *calls* `windows-rtx-build-test.yml` and
`windows-woa-build-test.yml` as reusable workflows, so a PR validation is one
run with the build and test jobs nested inside it — the approval, the audit
record and every job it authorised share a single page.

## The model

```
gate ---- author on allowlist ----> approval ----> authorize --+--> windows-rtx-build-test
   \                              (a maintainer)   (records    |
    `-- anyone else -> nothing                       who)       `--> windows-woa-build-test
                       at all
```

Three rules, and the first two are easy to read backwards:

- **Approval is always required.** The allowlist decides whether you are *asked*
  about a PR. It never lets one skip approval.
- **Not being on the allowlist is silent.** No request, no notification, no run.
  That is the point — it is what keeps the volume survivable.
- **An approved PR runs everything.** There is no per-PR choice of pipeline; the
  decision is whether this PR gets CI, not which CI it gets.

So there are two separate sets of people, in two different places:

| Set | Where | What it controls |
| --- | --- | --- |
| **Allowlist** — whose PRs raise a request | `UPSTREAM_PR_ALLOWLIST` repository **variable** | Which upstream authors you get asked about |
| **Approvers** — who can approve a request | `pr-ci-approval` environment's **Required reviewers** | Who is allowed to say yes |

### Why there is no per-pipeline selection

Routing on a label on the upstream PR was considered and does not work. A label
has to exist in `pytorch/pytorch` before it can be applied, which needs write
access there, and applying one needs triage permission — which the contributors
whose PRs this validates do not have. Creating and governing such a label is
upstream's call; their `.github/allowlist.yml` documents `ciflow/oot/<name>` for
trust level L3.

Capacity is therefore controlled by **who is on the allowlist** and by
**approval being mandatory**. If that needs to be tighter, narrow the subset
inputs passed to each pipeline in `upstream-pull.yml`.

## What is discarded before anything else

Two filters run ahead of the allowlist, because they are about whether the event
describes a PR worth looking at rather than about who may run one.

| Filter | Kept | Discarded |
| --- | --- | --- |
| Base branch | PRs targeting `main` | Anything else |
| Event action | `opened`, `synchronize` | `closed`, `reopened`, `labeled`, `edited`, `assigned`, and the rest |

The base-branch filter matters more than it sounds. Around a third of upstream's
open PRs target `gh/<user>/<n>/base` rather than `main` — those are **ghstack**
intermediates, the internal plumbing of someone's stacked PR, and testing the
intermediate means nothing. Release branches are excluded on the same grounds:
this validates changes headed for main.

`labeled` is deliberately **not** accepted. Upstream's bots and maintainers apply
several labels to a PR over its life (`open source`, `triaged`,
`release notes: ...`), and since no label selects anything here, accepting it
would raise a fresh approval request for a commit that had already been approved
every time one was added.

`synchronize` fires only when the head SHA changes: commits pushed, a
force-push, or the head being updated from its base. It does not fire for
comments, reviews, title edits or labels, and re-targeting a PR to a different
base is `edited`. So every accepted `synchronize` genuinely means there is
different code to test.

One logical push can still produce **more than one** event a second or two
apart — a ghstack push rewrites both a PR's head and its synthetic base. Nothing
tries to detect duplicates: the per-PR concurrency group cancels the in-flight
run when the next event arrives, so the last event wins and only the newest head
SHA is ever assessed.

Both decisions are shown in the `gate` summary, so a discarded dispatch says why.

## `PR_CI_MODE`: the master switch

A repository variable under **Settings → Secrets and variables → Actions →
Variables**. This is both the enable switch and the kill switch.

| Value | Behaviour |
| --- | --- |
| unset or `off` | Nothing happens. Every job is skipped, so a dispatch costs nothing and shows up as a skipped run. **This is the default.** |
| `live` | The allowlist applies, and an approved run starts both pipelines. |

Any other value fails the `gate` job loudly, so a typo can't read as "quietly do
nothing".

It has to be a **repository** variable, not an environment one. The mode is read
in `gate`'s job-level `if:`, and `gate` deliberately declares no environment
because it runs before the approval — environment-scoped variables only resolve
for jobs that reference that environment, so one would read as empty and skip
every dispatch while looking correctly configured. `UPSTREAM_PR_ALLOWLIST` is
repository-scoped for the same reason.

## Updating the allowlist

The list lives in a repository variable, not in this repo, so changing who you
get asked about needs no merge — and only people who can reach repository
settings can change it.

1. Go to **Settings → Secrets and variables → Actions → Variables**.
2. Edit **`UPSTREAM_PR_ALLOWLIST`**, or create it with that exact name.
3. Enter GitHub logins separated by any mix of commas, semicolons, newlines or
   spaces. All of these read identically:

   ```
   alice, bob, carol
   alice;bob;carol
   alice
   bob
   carol
   ```

4. Save. The next relayed PR event picks it up.

Before you edit it:

- **Matching is case-insensitive**, as logins are on GitHub.
- **Only whole logins match.** Listing `bob` does not admit `bobby`.
- **Wildcards do not work.** `*` is dropped with a warning. There is deliberately
  no way to spell "everyone": the relay forwards every upstream PR event, so an
  allow-all would mean approving each one by hand.
- **An empty or unset variable is not an outage.** It means no author's PRs are
  being picked up, which is the correct fail-closed default.
- **The list is never printed into a run summary**, only its entry count.
  Summaries outlive the variable they were read from.

## Approving a run

The run parks with a **Review deployments** prompt, and the reviewers on
`pr-ci-approval` get notified. A pending request appears in four places: a
GitHub notification, the run page (yellow *Waiting* state with a **Review
deployments** button), the Actions list, and the repository's Environments view.

1. Open the run under **Actions → upstream-pull**.
2. Read the `gate` summary: the PR author, the upstream PR number, the head SHA,
   and the pipelines it will start.
3. **Look at the PR's diff before approving.** Approving runs that code on
   self-hosted machines. This is the step the whole gate exists for.
4. Click **Review deployments**, tick `pr-ci-approval`, optionally leave a
   comment (it is recorded in the audit line), and **Approve and deploy**.

Consequences of approval being per-run:

- **You approve one commit, not a PR.** The run is pinned to the head SHA shown
  in `gate`. A later push gets its own run and its own approval.
- **A new push to the same PR cancels its waiting run**, so an approval granted
  to a superseded run is discarded with it. Approve the newest run for that PR.
  This is deliberate: approving stale code is the thing worth preventing.
- **Different PRs are independent.** Several can await approval at once, and
  several can build at once; nothing serializes them.
- **Rejecting is final for that run.** The pipelines are skipped and nothing is
  re-queued.
- **An unattended run expires** after GitHub's 30-day cap on a pending
  deployment.

## One-time setup: the `pr-ci-approval` environment

Without this environment, GitHub starts the `approval` job immediately and every
PR that reaches it is effectively auto-approved.

1. **Settings → Environments → New environment**, named exactly `pr-ci-approval`.
2. Tick **Required reviewers** and add the maintainers who may authorize PR CI
   (up to six users or teams).
3. Leave **Deployment branches** unrestricted. The workflow runs on the default
   branch, and a branch rule here only adds a second, confusing way to be
   blocked.
4. Save.

Required reviewers on a private repository need GitHub Team or Enterprise. On a
plan without them the environment is accepted but never gates anything — so
after setup, confirm that a PR actually parks before relying on it.

## What an approval starts

Both pipelines, every time. The list is the `PIPELINES` variable at the top of
`upstream-pull.yml`, which the audit record reports and
`scripts/pr-gate/tests/test_workflow_contract.py` keeps in step with the jobs
that call them:

```
PIPELINES: windows-rtx-build-test.yml windows-woa-build-test.yml
```

Each is called with `pytorch-ref` set to **the head SHA the gate validated and
you approved** — not the PR number. Both pipelines pass a full SHA straight
through to their build workflow, and a fork PR's head commit is fetchable from
`pytorch/pytorch` because GitHub republishes it under `refs/pull/*`. Pinning the
SHA means the run tests exactly what you approved, rather than re-resolving the
PR head and possibly building a commit that landed afterwards.

**A PR run cannot publish to the upstream HUD**, and this does not depend on an
opt-out flag. Both `prep` jobs leave `crcr-delivery-id` empty for a non-nightly
`pytorch-ref`, and both also clear it for any event that is not `schedule` or
`workflow_dispatch` — so a relay-driven run is excluded twice over. See
[HUD reporting](crcr-hud-reporting.md).

Note that `windows-woa-build-test.yml` still offers **no manual trigger**. It
gained a `workflow_call` arm for this path only, so its callers remain the
nightly schedule and an approved PR.

## What gets recorded

| Where | What |
| --- | --- |
| Run title | `PR #<n> -> <base> (<action>)`, so the Actions list is readable at a glance. |
| `gate` summary | Base branch, event action, PR author, allowlist verdict, entry count, the pipelines an approval would start, and whether a request was raised. |
| `authorize` summary | Author, approver, approval comment, PR number, head SHA, pipelines started, and any earlier rejection on the same run. |
| Run annotations | One `notice` naming the approver, the author, and what was started. |
| `inspect-dispatch` summary | The raw relay payload and dispatch type. |

The approver is read back from the run's own review history
(`GET /repos/{owner}/{repo}/actions/runs/{run_id}/approvals`), because GitHub
enforces environment approval but does not pass the approver's identity into the
run. If that read fails the approver is recorded as `unknown` and the job warns,
rather than failing an already-approved pipeline over a logging gap.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| No `upstream-pull` runs at all | The relay is not dispatching to this repo, or is using a dispatch type this workflow does not listen for. |
| Runs appear but every job is skipped | `PR_CI_MODE` is unset or `off`. That is the default. |
| `gate` red with "PR_CI_MODE is '...'" | The variable holds something other than `off` / `live`. |
| `gate` red with "no usable PR number" or "no usable head SHA" | The payload matched neither the nested nor the flat shape. `inspect-dispatch` prints what arrived. |
| `PR_CI_MODE` is set but every job still skips | It was added as an *environment* variable. It must be a repository variable. |
| No PR ever waits for approval | The `pr-ci-approval` environment is missing or has no required reviewers. |
| An allowlisted author raises no request | The relay sent the legacy flat payload, which carries no author field to match. |
| A PR you care about is skipped with "targets ... not main" | It is a ghstack intermediate or a release-branch PR. Test the top of the stack, which targets `main`. |
| A waiting request vanished before you could approve it | The PR was pushed to. That cancels its run and replaces it with one pinned to the newer head — approve the new one. |
| Two requests seconds apart for the same PR | One push can fire two events; a ghstack push rewrites both the head and the synthetic base. The concurrency group cancels the earlier run. |
| Approver shows as `unknown` | The review-history read failed. Confirm the workflow still grants `actions: read`. |

## Related

- `.github/workflows/upstream-pull.yml` — the router.
- `scripts/pr-gate/` — the allowlist decision and the audit record, with tests.
- [`docs/crcr-hud-reporting.md`](crcr-hud-reporting.md) — the other half of the
  relay integration: publishing *nightly* results upstream.
- [`docs/ci-details.md`](ci-details.md) — how the rest of the workflows are
  triggered, and the repository layout.
