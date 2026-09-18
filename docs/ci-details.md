<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: MIT
-->

# PyTorch OOT Windows CI — Architecture & Reference

Detailed reference for the workflows in this repository. For a high-level
overview and quick start, see the top-level [README](../README.md).

This repository provides NVIDIA PyTorch out-of-tree (OOT) CI on
self-hosted **Windows + NVIDIA RTX** (x86-64) and **Windows-on-Arm**
(arm64) runners. The workflows here implement
the downstream half of [RFC-0050: Cross-Repository CI Relay for PyTorch
Out-of-Tree Backends](https://github.com/pytorch/rfcs/blob/master/RFC-0050-Cross-Repository-CI-Relay-for-PyTorch-Out-of-Tree-Backends.md)
and mirror the in-tree shape of `pytorch/pytorch` PR
[#176678 - \[CI\]\[Windows\] Add NVIDIA RTX workflow](https://github.com/pytorch/pytorch/pull/176678).
Upstream covers a single configuration (Python 3.12, CUDA 12.8); this
repository expands the matrix so it can catch regressions
across multiple Python and CUDA toolkit combinations before they show up
upstream. Build/test logic itself comes entirely from PyTorch's in-tree
`.ci/pytorch/*.sh` scripts; this repo holds only the workflow wiring.

Build and test jobs run on self-hosted runners provided by NVIDIA
infrastructure. The lightweight jobs — lint, prep/ref-resolution, and
test-summary — run on GitHub-hosted `ubuntu-latest`.

## Windows-on-Arm

Alongside the RTX x86_64 flow, `windows-woa-build-test.yml` drives the reusable
`_woa-build.yml` and `_woa-test.yml` workflows on the `woa-arm64` runner pool.
See the [WoA operator guide](woa-ci.md) and
[WoA design and runner contract](woa-ci-plan.md) for the matrix, preinstalled
toolchain, persistent-runner cleanup, and operational details.

## Triggering workflows

There are three top-level workflows. Two of them run automatically on a
nightly `schedule`; the third is manual-only for now:

- **`windows-rtx-build-test.yml`** — full source build + test, nightly at
  `20 9 * * *` (14:50 IST). Also accepts `workflow_dispatch` for manual runs.
- **`windows-woa-build-test.yml`** — WoA (arm64) source build + test, nightly
  at `50 9 * * *` (15:20 IST). Scheduled only; a manual trigger is
  deliberately not offered.
- **`windows-rtx-wheel-test.yml`** — published-wheel test. Its nightly cron
  (`0 17 * * *` / 22:30 IST) is currently commented out, so the workflow runs
  on `workflow_dispatch` only.

Both active schedules sit after `pytorch/pytorch` cuts the day's `nightly`
commit, which over the 57 days to 2026-09-09 landed between 07:35 and 08:47
UTC. This matters because `prep` resolves the newest nightly at or before the
run's start — a cron that fires earlier does not wait for the day's commit, it
builds, tests and reports the previous day's again.

The reusable workflows (`_rtx-build.yml`, `_rtx-test.yml`, `_woa-build.yml`,
`_woa-test.yml`) are called by the orchestrators and are not run directly.

Separate from those three, **`upstream-pull.yml`** is driven by the upstream
relay rather than by anyone here: it validates a `pytorch/pytorch` pull request
whenever the relay dispatches one. Whether a given PR runs is decided by a
maintainer-controlled allowlist plus an explicit approval, after which it calls
both build/test orchestrators as reusable workflows, pinned to the approved
commit — see
[per-PR CI: the allowlist and maintainer approval](per-pr-ci-triggering.md).
This is also why `windows-rtx-build-test.yml` and `windows-woa-build-test.yml`
each carry a `workflow_call` trigger; it does not make either of them manually
startable.

## License and notices

This repository is released under MIT terms. See [LICENSE](../LICENSE) for
the project license, [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) for
third-party OSS notices.

## Workflows

| Workflow | Purpose | Triggers | Compute |
| --- | --- | --- | --- |
| `windows-rtx-wheel-test.yml`           | Each test cell checks out `pytorch/pytorch` at `pytorch-ref` (default `nightly`) via `actions/checkout@v7` (which resolves the branch to a concrete commit), records the actual HEAD SHA + commit date into the cell's job summary, then greps `download.pytorch.org/whl/nightly/torch/` for the wheel whose filename carries that exact `devYYYYMMDD` tag together with the matrix `cu<label>` / `cp<pyshort>` tags and `pip install`s the resolved absolute URL before running `.ci/pytorch/win-test.sh`. Fails fast if no matching wheel exists, so the wheel under test always shares its commit date with the pytorch source on disk. No preflight job, no artifact transit. | `workflow_dispatch`; the nightly cron is currently commented out | `_rtx-test.yml` (sm89 + sm120 in one matrix) |
| `windows-rtx-build-test.yml`            | Full source build (multi-arch wheel) + test. Manual runs can narrow the matrix via subset filters and target a `pytorch-ref` or `pytorch-pr`. Also carries the parked path for RFC-0050 events. | `schedule` (`20 9 * * *` = 14:50 IST), `workflow_dispatch`, `repository_dispatch:[pytorch-pr-trigger]` (parked behind `dispatch-gate`) | `prep` -> `_rtx-build.yml` -> `_rtx-test.yml` (sm89 + sm120 in one matrix); `report-*-crcr` |
| `windows-woa-build-test.yml` | Builds and tests the WoA wheel matrix from source on the shared arm64 pool. | `schedule` (`50 9 * * *` = 15:20 IST); no manual trigger | `prep` -> `_woa-build.yml` -> `_woa-test.yml` -> `test-summary`; `report-*-crcr` |

Both build/test orchestrators end in two terminal `report-*-crcr` jobs that
publish the nightly's results to the upstream PyTorch HUD. They hang off the
matrix rather than feeding it, so a reporting failure can never skip a build or
a test. See [HUD reporting](crcr-hud-reporting.md) for the row names, how a
conclusion is decided, and why re-runs must resolve the same upstream SHA.

Both RTX workflows fan out across `(config)` for builds and
`(config x arch)` for tests. **Sharding is not a top-level axis on
either orchestrator** - it lives inside `_rtx-test.yml`'s own
`strategy.matrix.shard`, so one call to the reusable workflow ==
one `(config, arch)` test cell, and each call internally spawns the
5 shard runners nested underneath it. This matches upstream
`_win-rtx-test.yml` (PR #176678) where the `test-matrix` JSON drives
sharding inside the reusable workflow rather than on the caller.

`config` is a paired `{python, cuda}` entry rather than independent
`python` and `cuda` axes, because the runner pool is allocated per
(python, cuda) combination - py312/cu130 and py312/cu132 are
different machines, so the matrix enumerates the actual pairings
rather than blindly cross-multiplying.

Cell names mirror `pytorch/pytorch`'s generated
`windows-binary-wheel` nightly (`wheel-py3_10-cuda13_0-build` /
`wheel-py3_10-cuda13_0-test`). Each `config:` entry carries a
precomputed `build_name` (`wheel-py312-cu130`, etc.) so the
job-level `name:` collapses to a one-token reference exactly like
upstream's `name: ${{ matrix.build_name }}-build`:

| Job | Cell name template | Example cell |
| --- | --- | --- |
| orchestrator `build`            | `<build_name>-build`        | `wheel-py312-cu130-build` |
| orchestrator `test`             | `<build_name>-<arch>-test`  | `wheel-py312-cu130-sm89-test` |
| `_rtx-test.yml`'s inner shards  | `test (shard <N>/5)`        | nested under each `*-test` cell |

GitHub groups matrix cells alphabetically by name, so leading with
`wheel-<py>-<cu>` keeps each wheel's two arch fanouts adjacent and
also lines up a wheel-test row alongside its windows-rtx-build-test
counterpart in cross-workflow dashboards.

`.ci/pytorch/win-test.sh` (via `test/run_test.py`) honours the
`SHARD_NUMBER` / `NUM_TEST_SHARDS` / `TEST_CONFIG` env vars set
inside `_rtx-test.yml` to run just its slice.

```
windows-rtx-build-test.yml:                          windows-rtx-wheel-test.yml:

  build  matrix( config )                         (no preflight job)
      |   (3 cells)                                test  matrix( config x arch )
      |   multi-arch wheel + SHA sidecar                  (3 x 2 = 6 cells)
      |   uploaded as one artifact per cell
      |                                                  each cell calls
      +-> test  matrix( config x arch )                  _rtx-test.yml, which
                (3 x 2 = 6 cells)                        internally fans out
                  each cell calls _rtx-test.yml,         5 shard runners.
                  which internally fans out 5
                  shard runners (30 runners total).      Inside each runner:
                                                           - checkout pytorch@nightly
                  Inside each runner:                      - grep public index for the
                    - pip install the build's wheel          matching devYYYYMMDD wheel
                      artifact                             - pip install URL
                    - run shard N of 5                     - run shard N of 5

UI grouping in both workflows (orchestrator level):
  wheel-py312-cu130-build                  (windows-rtx-build-test only)
  wheel-py312-cu130-sm89-test              ... drill in for 5 shard cells
  wheel-py312-cu130-sm120-test             ... drill in for 5 shard cells
  wheel-py312-cu132-build                  (windows-rtx-build-test only)
  wheel-py312-cu132-sm89-test
  ...
```

`_rtx-test.yml` accepts two install paths and routes between them based
on which inputs the orchestrator provided:

| Install path | When | Required inputs | Checkout ref from | Install source |
| --- | --- | --- | --- | --- |
| **artifact** (path A) | source build | `wheel-artifact` | SHA in `built_pytorch_sha.txt` inside the artifact | `pip install ./artifact/*.whl` |
| **pip-index** (path B) | nightly wheel | `pytorch-ref` (+ optional `wheel-index-url`, default `https://download.pytorch.org/whl/nightly/torch/`) | `pytorch-ref` passed verbatim (typically `nightly`); `actions/checkout@v7` resolves it | Wheel URL grepped from the index by checked-out commit's `devYYYYMMDD` + matrix `cu<label>` / `cp<pyshort>` tags |

In both paths the test job records the actual `git rev-parse HEAD` +
commit date of the checkout into its Step Summary, so each cell logs
"what nightly did I test" without needing a centralized preflight.
This keeps `_rtx-build.yml` as the only producer that needs to ship a
wheel through GitHub artifact storage. The nightly path avoids the
fetch/upload/download round-trip entirely - the test runner that
resolves the ref is the same runner that pip-installs and tests.

The path-B resolver fails fast if the index has no wheel for the
checked-out commit's date - that is the signal that the nightly wheel
for the source just pulled is not yet published, and any install
would otherwise silently fall back to an older wheel that disagrees
with the source tree on disk.

## Default matrix

`config` (paired entries — each one corresponds to a real allocated
runner; add/remove entries to match the runner pool):

| python | cuda toolkit | python-label | cuda-label |
| --- | --- | --- | --- |
| 3.12 | 13.0 | `py312` | `cu130` |
| 3.12 | 13.2 | `py312` | `cu132` |
| 3.13 | 13.2 | `py313` | `cu132` |

Plus `arch: [sm89, sm120]` on the orchestrator's test job, with the
5-shard fanout living inside `_rtx-test.yml`
(`strategy.matrix.shard: [1, 2, 3, 4, 5]`, `NUM_TEST_SHARDS: "5"`),
matching PR #176678.

Per source-build run that's **3 build jobs + 6 orchestrator-level
test cells** (3 configs x 2 archs); each test cell expands to 5
nested shard runners, so the actual runner count is `3 + 6 * 5 = 33`
GH Actions runner jobs. The wheel-test run is **6 orchestrator-
level test cells** (30 runners after the internal shard fanout) - no
preflight, no per-cell wheel producer.

`TORCH_CUDA_ARCH_LIST` is set per `arch` matrix entry (`8.9` for sm89,
`12.0` for sm120), and `runner-base` likewise (`rtx-40x0-test` vs
`rtx-50x0-test`). The build wheel itself is multi-arch (`8.9;12.0`) so
a single producer feeds both architectures.

## Runner model

Runners are **ephemeral, pre-prepped images**. The image has the right
Python, CUDA toolkit, MSVC build tools, sccache, magma, cmake, ninja, and
the standard PyTorch test runtime pre-installed and on `PATH`. The
workflows perform **zero** in-job environment setup. Each matrix cell is
routed to its image via the runner-label set:

| Job kind | Label set |
| --- | --- |
| `build` (source-build wheel producer)           | `[rtx-build, <python-label>, <cuda-label>]` |
| `test` cells where `matrix.arch.name == sm89`   | `[rtx-40x0-test, <python-label>, <cuda-label>]` |
| `test` cells where `matrix.arch.name == sm120`  | `[rtx-50x0-test, <python-label>, <cuda-label>]` |
| `inspect-dispatch` (parked RFC-0050 arm)        | `[rtx-build]` (any free build runner) |
| `prep`, `test-summary`, `lint`                  | `ubuntu-latest` (GitHub-hosted) |

The narrow labels (`rtx-build`, `rtx-40x0-test`, `rtx-50x0-test`,
`py3xx`, `cu1xx`) are unique to the self-hosted Windows pool, so the
GitHub auto-tags (`self-hosted`, `Windows`, `X64`) that the runner
agent applies are redundant in the AND filter and are deliberately
left off `runs-on:` everywhere.

For example, the sm120 test cell for Python 3.13 + CUDA 13.0 needs an
image registered as `[rtx-50x0-test, py313, cu130]` (plus whatever
auto-tags the runner agent adds).

## What the runner image must already contain

Because there is no in-job setup, the pre-prepped image carries everything the
PyTorch CI scripts (`.ci/pytorch/win-build.sh`, `.ci/pytorch/win-test.sh`,
`.ci/pytorch/win-test-helpers/**`) expect to find. Concretely:

- Python (matching matrix cell, on `PATH` as `python`)
- CUDA toolkit (matching matrix cell) and a recent enough GPU driver
- cuDNN, NCCL (where applicable) bundled with the toolkit
- Visual Studio Build Tools / MSVC (`cl.exe` reachable through `vcvarsall.bat`)
- Git for Windows (provides `bash`, `git`, `curl`)
- ninja, cmake, sccache, magma binaries
- All Python deps from `pytorch/.ci/docker/requirements-ci.txt` for the
  matching Python version (numba 0.64.0+, pytest, expecttest, hypothesis,
  numpy, ...; see `pytorch/pytorch` PR #176678 review thread for the
  current pin set)
- `nvidia-smi` on `PATH`
- Windows PowerShell 5.1 (the in-box `powershell.exe`) is sufficient
  for the runner-diagnostics composite actions. PowerShell 7+ (`pwsh`) is
  NOT required on the RTX pool - every script those actions invoke sticks
  to cmdlets and language features available in 5.1.

PyTorch's in-tree CI scripts cover build, install, and test end-to-end on
the RTX pool. The repo-local helpers around them are the runner-diagnostics
monitor described [below](#runner-diagnostics), the test-summary and
test-stats scripts, and the vendored WoA build/test library under
`tools/woa-build/`.

## Repository layout

```
.github/
  workflows/
    windows-rtx-build-test.yml       # full source build + test (nightly + manual; parked PR path)
    windows-rtx-wheel-test.yml       # published-wheel test (manual; cron commented out)
    _rtx-build.yml                   # reusable: build source (.ci/pytorch/win-build.sh), uploads wheel artifact
    _rtx-test.yml                    # reusable: test a wheel (artifact OR pip-index install path)
    windows-woa-build-test.yml       # WoA arm64 source build + test (nightly; workflow_call for PR runs)
    _woa-build.yml                   # reusable WoA source build
    _woa-test.yml                    # reusable WoA wheel tests
    lint.yml                         # PR-time YAML and PowerShell lint
    upstream-pull.yml                # relay-driven per-PR validation (allowlist + approval)
  actions/
    start-runner-diagnostics/        # composite: spawn monitor.ps1 in background
    stop-runner-diagnostics/         # composite: signal stop, flush, summarise
    upload-local-artifact/           # composite: stage an artifact on the runner host
    download-local-artifact/         # composite: retrieve a host-staged artifact
    inspect-dispatch-event/          # composite: print the repository_dispatch payload
    woa-preflight-build/             # verify WoA build toolchain
    woa-preflight-test/              # verify WoA test environment
    woa-create-venv/                 # create a fresh per-job arm64 venv
    woa-strict-clean/                # clean persistent-runner state
scripts/
  runner-diagnostics/
    monitor.ps1                      # background sampler (host + GPU JSONL)
  test-summary/                      # aggregate + parse shard failures for the summary job
  test-stats/                        # committed test times that seed shard balancing
  local-artifact/                    # host-local artifact staging helpers
  dispatch-event/                    # repository_dispatch payload summary
  pr-gate/                           # PR-author allowlist decision + authorization audit record
tools/
  woa-build/                         # vendored PowerShell WoA build/test library
```

## Customising the matrix

The matrix work in each orchestrator lives in at most two jobs - `build`
and `test` (`windows-rtx-wheel-test.yml` has only `test`, since it
installs a published wheel instead of producing one). The surrounding
`prep`, `test-summary`, and `inspect-dispatch` jobs are single cells and
carry no matrix. The orchestrator's test matrix is 2-dimensional
(`config x arch`); the shard fanout lives one layer down in
`_rtx-test.yml`:

```yaml
# Orchestrator (windows-rtx-build-test.yml / windows-rtx-wheel-test.yml)
matrix:
  config:                      # paired {python, cuda} entries; each one
    - { python: { version: "3.12", label: "py312" },  #   corresponds to an actual allocated
        cuda:   { version: "13.0", label: "cu130" },  #   runner. Add/remove lines freely.
        build_name: "wheel-py312-cu130" }
    - { python: { version: "3.12", label: "py312" },
        cuda:   { version: "13.2", label: "cu132" },
        build_name: "wheel-py312-cu132" }
    # ... etc
  arch:                        # 2 entries, each carries runner-base
    - { name: sm89,  runner: rtx-40x0-test, arch_list: "8.9"  }
    - { name: sm120, runner: rtx-50x0-test, arch_list: "12.0" }

# _rtx-test.yml (reusable; one call per orchestrator test cell)
strategy:
  matrix:
    shard: [1, 2, 3, 4, 5]     # 5 shards per (config, arch); NUM_TEST_SHARDS env is "5"
```

In `windows-rtx-build-test.yml`, the `config` list is declared on
the `build` job (`&config` anchor) and re-used on the `test` job
(`*config`). In `windows-rtx-wheel-test.yml` the list lives directly on
the `test` job since there is no build to share it with.

To add or remove cells:
- **config axis** (a python+cuda pairing): edit the `config:` list in
  one place per orchestrator. Each entry is `{ python: {version,
  label}, cuda: {version, label}, build_name: ... }`. Because the
  matrix enumerates only the pairings you put in, dropping an
  unsupported combination (say `py313` + `cu130` if no machine for it
  exists) is just a line delete - no `exclude:` clause needed.
- **arch axis**: edit the `arch:` list on the `test` job. Each entry
  is a `{ name, runner, arch_list }` triple - `runner` becomes the
  fourth runner label, `arch_list` becomes `TORCH_CUDA_ARCH_LIST` for
  that cell.
- **shard count**: edit `_rtx-test.yml` in two places - the
  `strategy.matrix.shard` list and the `NUM_TEST_SHARDS` env literal.
  Orchestrators are agnostic to the shard count.
- **per-event matrix filters** (`workflow_dispatch` only): both
  orchestrators expose three comma-separated subset inputs and forward
  them verbatim via `with:` to the called reusable workflows
  (`_rtx-build.yml` / `_rtx-test.yml`), whose own job-level `if:`
  performs the match against the cell's own `python-version`,
  `cuda-version`, and `arch-name` inputs. The filter lives one layer
  down because GitHub Actions disallows `matrix.*` in the `if:` of a
  job that calls a reusable workflow. Schedule and
  `repository_dispatch` runs always cover every cell (the orchestrator
  forwards the empty string, which disables the corresponding filter
  dimension in the reusable workflow).

  | Input | Default | Filters |
  | --- | --- | --- |
  | `python-versions`    | `3.12,3.13`   | `build` + `test` (matches the cell's `python-version`) |
  | `cuda-versions`      | `13.0,13.2`   | `build` + `test` (matches the cell's `cuda-version`)   |
  | `test-architectures` | `sm89,sm120`  | `test` only (matches the cell's `arch-name`)           |

  Cells dropped by the filter show up in the GitHub UI with their
  inner reusable-workflow job in the "skipped" state, so a manual run
  that only covered py3.12 / cu13.0 still leaves an audit trail of
  every other slot as "this cell exists, was deliberately not
  exercised".

## Test environment variables

`_rtx-test.yml` exports the subset of PR #176678's test env block that
applies here (no AWS, no `filter-test-configs`,
no `get-workflow-job-id`):

| Scope | Variable | Source |
| --- | --- | --- |
| job  | `BUILD_ENVIRONMENT`, `PYTHON_VERSION`, `CUDA_VERSION`, `TORCH_CUDA_ARCH_LIST` | matrix cell |
| job  | `USE_CUDA=1`, `INSTALL_WINDOWS_SDK=0`, `CONTINUE_THROUGH_ERROR=1`, `PYTORCH_TEST_WITH_SLOW=0`, `CI=1` | static |
| job  | `VC_PRODUCT=BuildTools`, `VC_YEAR=2022`, `VS_VERSION=17.4.1`, `VC_VERSION=""` | MSVC tooling info |
| job  | `PIP_RETRIES=8`, `PIP_DEFAULT_TIMEOUT=60` | pip resilience for the test-harness install |
| job  | `PER_TEST_TIMEOUT_SEC=900`, `PER_PROCESS_TIMEOUT_SEC=2700`, `RUN_TEST_TIMEOUT_SEC=9900` | the bounds that hold a hung shard - see [Timeout bounds](#timeout-bounds) |
| job  | `PER_PROCESS_TIMEOUT_LOG` | `test/test-reports/per-process-bound.jsonl`; unset disables the records |
| job  | `AWS_EC2_METADATA_DISABLED=true` | suppresses a dead S3 telemetry probe - see below |
| step | `SHARD_NUMBER` | `_rtx-test.yml`'s internal `matrix.shard` |
| step | `NUM_TEST_SHARDS` | static (`"5"`, matches the shard list length) |
| step | `TEST_CONFIG` | `inputs.test-config` (default `"default"`) |
| step | `PYTORCH_FINAL_PACKAGE_DIR` | `${{ github.workspace }}/artifact` |
| step | `PYTHONPATH` | `scripts/test-bounds/pythonpath`, so `site` picks up our `sitecustomize.py` |
| step | `PR_NUMBER`, `SHA1` | `repository_dispatch` payload or PR context |
| step | `GITHUB_REPOSITORY` / `_WORKFLOW` / `_JOB` / `_RUN_ID` / `_RUN_NUMBER` / `_RUN_ATTEMPT` | `github.*` context |

`AWS_EC2_METADATA_DISABLED` is there because `run_test.py` tries to upload each
batch of test reports to pytorch's S3 bucket. There are no credentials on these
runners, so the upload cannot succeed and does not need to - nothing reads it.
But `boto3`, finding no credentials, next asks the EC2 instance metadata
service for them, and on a host that is not an EC2 instance there is nothing
listening on `169.254.169.254` to refuse the connection. It waits for the
connect to time out instead, once per batch. Setting this makes `boto3` skip
that probe and give up at once. The test step also filters the resulting
`Failed to parse and upload json test reports: Unable to locate credentials`
line out of the log, since the upload is expected to fail.

## Test sharding

`test/run_test.py` assigns test files to the 5 shards itself, using per-file
timings it reads from `<pytorch>/.additional_ci_files/test-times.json`. Upstream
that file is downloaded from test-infra, which has no data for an out-of-tree
build env - hence the benign warning every shard logs:

```
Gathered no stats from artifacts for win-rtx-sm89 build env and default
test config. Using default job name and default test config instead.
```

The fallback to `default`/`default` is the intended path here: the
`Seed test-time stats` step runs `scripts/test-stats/seed_test_stats.py`, which
copies our committed `scripts/test-stats/data/*.json` into that folder under
exactly those keys. Every shard reads the same committed JSON, so all 5 agree on
the split without coordinating.

Timings do two things beyond balance. `calculate_shards` bin-packs by cost only
for a file whose time is known; an unknown file falls back to round-robin
(`_get_min_sharded_job` in `tools/testing/test_selections.py`), which ignores
cost. And any file over the 10-minute `THRESHOLD` is split into
`ceil(duration / 600)` pytest shards spread across jobs, so `test_meta` runs as
15 pieces of ~9.5 min rather than one atomic 2.4-hour file.

So that no file drops out of cost-based packing, `seed_test_stats.py` backfills
an entry for every `test_*.py` in the checkout that our data has never measured,
at the median of the times we do have. `--no-backfill` disables the backfill.
Each run states the outcome:

```
sharding coverage: 1271 test file(s) in the checkout, 633 backfilled at 15.6s,
0 left without a time
```

The last number should always be `0`. The middle one counts the whole `test/`
tree, most of which this CI never selects, so it is a poor drift signal - for
that, compare the files a run actually executed against the committed data.

### Timeout bounds

Four bounds apply to an x86 test shard, at descending granularity. The arm64
shards carry the same set at different values, and take their per-test and
per-shard bounds from the vendored harness rather than from the workflow - see
[WoA timeout bounds](woa-ci.md#timeout-bounds).

| Bound | Where | Value | On expiry |
| --- | --- | --- | --- |
| per test | `PYTEST_ADDOPTS=--timeout=... --timeout-method=thread` | 15 min | fails that test; the shard carries on |
| per test-file process | `PER_PROCESS_TIMEOUT_SEC`, armed by `scripts/test-bounds/pythonpath/sitecustomize.py` | 45 min | dumps all thread stacks, kills that file's process tree; the shard carries on |
| per test file | `run_test.py`'s own subprocess timeout (`THRESHOLD * 3`) | 30 min | **nothing - inert on Windows, see below** |
| per shard | in-step watchdog (`RUN_TEST_TIMEOUT_SEC`) | 165 min | `taskkill`s the test processes and fails the shard |

Do not rely on the per-file bound. It is armed only when `run_test.py` knows the
file's expected duration and only for the serial pytest invocation, and on
Windows it cannot kill anything even then: its expiry path calls
`Popen.send_signal(signal.SIGINT)`, which Windows rejects with `ValueError`
before reaching the `p.kill()` below it, leaving the handler blocked in
`finally: p.wait()` on a live child. That turns a timeout into a permanent hang.
It is an upstream pytorch defect with no environment-variable workaround, so
treat the bound as absent.

`PYTEST_ADDOPTS` only covers a test that is running, so the per-process bound is
what covers the rest of a test file's life:

| Window | per test | per process | per file | per shard |
| --- | --- | --- | --- | --- |
| inside a test (setup, call, teardown) | yes | yes | inert | yes |
| import and collection, before the first test | no | yes | inert | yes |
| session teardown, interpreter exit, CUDA context destruction | no | yes | inert | yes |
| between the serial and parallel invocations | n/a | yes | inert | yes |
| in `run_test.py` itself, or a non-Python step | no | no | no | yes |

The per-process bound lives in a `sitecustomize.py`, which `site` imports at
interpreter startup - hence the coverage of import, collection and teardown. It
is on `PYTHONPATH` in the test step only, and arms only in a process whose
`argv[0]` is a `test_*.py` file, so `run_test.py` itself is never bounded. Each
shard logs `per-process bound: <n>s, armed from <path>` once, or a `::warning::`
if the file is not on the path. Setting `PER_PROCESS_TIMEOUT_SEC` to `0` or
leaving it unset disables it.

Because the bound is silent while armed, a run in which nothing hangs cannot
otherwise be told apart from one where the module was never imported. So each
armed process appends a line to `PER_PROCESS_TIMEOUT_LOG`, and a second one if
it fires; the `Report per-process bound coverage` step turns those into
`per-process bound: armed in <n> test-file process(es), fired <m> time(s)` and
warns if `<n>` is `0`. The file rides along in the `test-reports` artifact. It
is `.jsonl` rather than `.log` because `parse_failures.py` scans that tree for
`*.log` / `*.txt` when hunting failures.

Both bounds are sized from measured runs. `RUN_TEST_TIMEOUT_SEC` is 165 min
against a slowest clean shard of 106 min and a slowest guarded one of 112 min.
`PER_PROCESS_TIMEOUT_SEC` is 45 min against a slowest single invocation of
26.6 min - compare it per *invocation*, not per file, since `run_test.py` runs
each file twice and pytest-shards the big ones, so a file's total can exceed it
legitimately (`test_meta` totals ~142 min).

### Refreshing the stats

The data is a snapshot and drifts as the upstream suite changes. Drift costs
balance, not safety - an unmeasured file is backfilled, so it is still packed on
cost, just from a guess rather than a measurement. To regenerate from a
completed run:

1. Download each shard's log for one `(config, arch)` cell - either
   `gh api repos/NVIDIA/pytorch-windows-ci/actions/jobs/<job-id>/logs`, or the
   `run_test_shard<N>.log` inside that shard's `test-reports-*` artifact.
2. Optionally extract the `test-reports-*` artifacts too; they are the only
   source of per-class times.
3. Run the generator, passing **every** shard of the run so that pytest-sharded
   files are seen whole:

```bash
python scripts/test-stats/gen_test_stats.py \
  --log-dir ./logs --report-dir ./reports/shard1 ... --report-dir ./reports/shard5
```

4. Commit the regenerated `scripts/test-stats/data/*.json`.

Use a run whose shards all completed: a cancelled shard truncates its log, and
the generator can only scale a partially-observed file back up to an estimate.

## Runner diagnostics

Each test job spawns `scripts/runner-diagnostics/monitor.ps1` (resolved
by `start-runner-diagnostics` from `$GITHUB_ACTION_PATH`) in the
background while
`.ci/pytorch/win-test.sh` runs in the foreground. It writes one
artifact per cell, `runner-diagnostics-<env>-<py>-<cu>-<run_id>-<attempt>`,
14-day retention, containing:

```
spec-snapshot.json   host / CPU / RAM / disk / driver / GPU / Python / nvcc
system.jsonl         CPU %, mem MiB, disk free / used GB, top 5 procs by WS
gpu.jsonl            per-GPU util, mem, temp, power, SM / mem clocks
monitor.log          start / stop bookends + sample count
```

Pipe the JSONL files through `jq` / `pandas` to plot pressure around a
failure. To tune the interval or relocate the output dir, edit the
`with:` block in `_rtx-test.yml` (`start-runner-diagnostics` accepts
`interval-seconds` and `output-dir` inputs).

## Test summaries

The RTX and WoA orchestrators run a final `test-summary` job on
`ubuntu-latest` after all test cells settle. It uses
`scripts/test-summary/aggregate_failures.py` to summarize failed jobs and
`scripts/test-summary/parse_failures.py` to combine failures from downloaded
shard reports. The summary job is informational; the individual test jobs
remain responsible for the workflow result.

## RFC-0050 mapping

| RFC concept | This repo |
| --- | --- |
| Downstream CI on real PR-time events | `windows-rtx-build-test.yml` subscribes to `repository_dispatch:[pytorch-pr-trigger]`. The arm is parked behind `dispatch-gate`: `inspect-dispatch` prints the payload and the build/test/summary jobs stay dormant. |
| `concurrency: upstream-pr-<pr_number>` | `windows-rtx-build-test.yml` keys `concurrency.group` on `client_payload.pr_number` when present |
| `pytorch/actions/checkout-pr@v1` (RFC Action #1) | Used as-is in `_rtx-build.yml` for `repository_dispatch`; falls back to `actions/checkout@v7` against `pytorch/pytorch@<ref>` for schedule / manual runs |
| `pytorch/actions/report-ci-result@v1` (RFC Action #2) | Not yet wired — result acknowledgement is pending publication of the upstream action |

## Local workflow validation

```bash
python -m pip install "PyYAML>=6" "check-jsonschema>=0.29"
check-jsonschema --builtin-schema vendor.github-workflows .github/workflows/*.yml
```

The same checks run automatically in `lint.yml` on every PR.
