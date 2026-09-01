<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: MIT
-->

# PyTorch OOT Windows CI — Architecture & Reference

Detailed reference for the workflows in this repository. For a high-level
overview and quick start, see the top-level [README](../README.md).

This repository hosts the GitHub Actions workflows that build and test PyTorch
on NVIDIA's self-hosted Windows + RTX runner pool. It implements the downstream
half of [RFC-0050: Cross-Repository CI Relay for PyTorch Out-of-Tree
Backends](https://github.com/pytorch/rfcs/blob/master/RFC-0050-Cross-Repository-CI-Relay-for-PyTorch-Out-of-Tree-Backends.md)
and mirrors the in-tree shape of `pytorch/pytorch` PR
[#176678 - \[CI\]\[Windows\] Add NVIDIA RTX workflow](https://github.com/pytorch/pytorch/pull/176678).

Upstream covers a single configuration (Python 3.12, CUDA 12.8); this repo
deliberately expands the matrix so regressions across multiple Python and CUDA
toolkit combinations are caught before they show up upstream. The build/test
logic itself comes entirely from PyTorch's in-tree `.ci/pytorch/*.sh` scripts —
this repo holds only the workflow wiring.

**Every job runs on a self-hosted runner provided by NVIDIA infrastructure.**
There are no GitHub-hosted (cloud) runs anywhere in this repo.

## Triggering workflows

The two top-level workflows run automatically on a nightly `schedule`:

- **`windows-rtx-build-test.yml`** — full source build + test (nightly at
  `5 3 * * *` / 08:35 IST).
- **`windows-rtx-wheel-test.yml`** — nightly published-wheel smoke test
  (`0 17 * * *` / 22:30 IST), installing the matching `download.pytorch.org`
  nightly wheel rather than building from source.

## Runner requirements

Runners are **ephemeral, pre-prepped images**. The workflows perform **zero**
in-job environment setup, so the image must already carry everything the
PyTorch CI scripts (`.ci/pytorch/win-build.sh`, `.ci/pytorch/win-test.sh`,
`.ci/pytorch/win-test-helpers/**`) expect:

- Python (matching the matrix cell, on `PATH` as `python`)
- CUDA toolkit (matching the matrix cell) and a recent enough GPU driver
- cuDNN, NCCL (where applicable) bundled with the toolkit
- Visual Studio Build Tools / MSVC (`cl.exe` reachable through `vcvarsall.bat`)
- Git for Windows (provides `bash`, `git`, `curl`)
- ninja, cmake, sccache, magma binaries
- All Python deps from `pytorch/.ci/docker/requirements-ci.txt` for the matching
  Python version (numba 0.64.0+, pytest, expecttest, hypothesis, numpy, ...; see
  `pytorch/pytorch` PR #176678 review thread for the current pin set)
- `nvidia-smi` on `PATH`
- Windows PowerShell 5.1 (the in-box `powershell.exe`) is sufficient for the
  runner-diagnostics composite actions. PowerShell 7+ (`pwsh`) is NOT required —
  every script in this repo sticks to cmdlets and language features available
  in 5.1.

Each matrix cell is routed to its image via the runner-label set:

| Job kind | Label set |
| --- | --- |
| `build` (source-build wheel producer)           | `[rtx-build, <python-label>, <cuda-label>]` |
| `test` cells where `matrix.arch.name == sm89`   | `[rtx-40x0-test, <python-label>, <cuda-label>]` |
| `test` cells where `matrix.arch.name == sm120`  | `[rtx-50x0-test, <python-label>, <cuda-label>]` |

The narrow labels (`rtx-build`, `rtx-40x0-test`, `rtx-50x0-test`, `py3xx`,
`cu1xx`) are unique to the self-hosted Windows pool, so the GitHub auto-tags
(`self-hosted`, `Windows`, `X64`) that the runner agent applies are redundant in
the AND filter and are deliberately left off `runs-on:` everywhere. For example,
the sm120 test cell for Python 3.13 + CUDA 13.0 needs an image registered as
`[rtx-50x0-test, py313, cu130]` (plus whatever auto-tags the runner agent adds).

## Workflows

| Workflow | Purpose | Triggers | Compute |
| --- | --- | --- | --- |
| `windows-rtx-wheel-test.yml`           | Each test cell checks out `pytorch/pytorch` at `pytorch-ref` (default `nightly`) via `actions/checkout@v7` (which resolves the branch to a concrete commit), records the actual HEAD SHA + commit date into the cell's job summary, then greps `download.pytorch.org/whl/nightly/torch/` for the wheel whose filename carries that exact `devYYYYMMDD` tag together with the matrix `cu<label>` / `cp<pyshort>` tags and `pip install`s the resolved absolute URL before running `.ci/pytorch/win-test.sh`. Fails fast if no matching wheel exists, so the wheel under test always shares its commit date with the pytorch source on disk. No preflight job, no artifact transit. | `schedule` (`0 17 * * *` = 22:30 IST) | `_rtx-test.yml` (sm89 + sm120 in one matrix) |
| `windows-rtx-build-test.yml`            | Full source build (multi-arch wheel) + test, scheduled nightly. Also carries the parked path for real RFC-0050 PR-time events. | `schedule` (`5 3 * * *` = 08:35 IST) | `prep` -> `_rtx-build.yml` -> `_rtx-test.yml` (sm89 + sm120 in one matrix) |

Both nightly workflows fan out across `(config)` for builds and
`(config x arch)` for tests. **Sharding is not a top-level axis on either
orchestrator** - it lives inside `_rtx-test.yml`'s own `strategy.matrix.shard`,
so one call to the reusable workflow == one `(config, arch)` test cell, and each
call internally spawns the 5 shard runners nested underneath it. This matches
upstream `_win-rtx-test.yml` (PR #176678) where the `test-matrix` JSON drives
sharding inside the reusable workflow rather than on the caller.

`config` is a paired `{python, cuda}` entry rather than independent `python` and
`cuda` axes, because the runner pool is allocated per (python, cuda)
combination - py312/cu130 and py312/cu132 are different machines, so the matrix
enumerates the actual pairings rather than blindly cross-multiplying.

## Job naming

Cell names mirror `pytorch/pytorch`'s generated `windows-binary-wheel` nightly
(`wheel-py3_10-cuda13_0-build` / `wheel-py3_10-cuda13_0-test`). Each `config:`
entry carries a precomputed `build_name` (`wheel-py312-cu130`, etc.) so the
job-level `name:` collapses to a one-token reference exactly like upstream's
`name: ${{ matrix.build_name }}-build`:

| Job | Cell name template | Example cell |
| --- | --- | --- |
| orchestrator `build`            | `<build_name>-build`        | `wheel-py312-cu130-build` |
| orchestrator `test`             | `<build_name>-<arch>-test`  | `wheel-py312-cu130-sm89-test` |
| `_rtx-test.yml`'s inner shards  | `test (shard <N>/5)`        | nested under each `*-test` cell |

GitHub groups matrix cells alphabetically by name, so leading with
`wheel-<py>-<cu>` keeps each wheel's two arch fanouts adjacent and also lines up
a wheel-test row alongside its windows-rtx-build-test counterpart in
cross-workflow dashboards.

`.ci/pytorch/win-test.sh` (via `test/run_test.py`) honours the `SHARD_NUMBER` /
`NUM_TEST_SHARDS` / `TEST_CONFIG` env vars set inside `_rtx-test.yml` to run just
its slice.

```
windows-rtx-build-test.yml:                          windows-rtx-wheel-test.yml:

  build  matrix( config )                         (no preflight job)
      |   (3 cells)                                test  matrix( config x arch )
      |   multi-arch wheel + SHA sidecar                  (3 x 2 = 6 cells)
      |   (artifact upload currently disabled)
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

## Install paths

`_rtx-test.yml` accepts two install paths and routes between them based on which
inputs the orchestrator provided:

| Install path | When | Required inputs | Checkout ref from | Install source |
| --- | --- | --- | --- | --- |
| **artifact** (path A) | source build | `wheel-artifact` | SHA in `built_pytorch_sha.txt` inside the artifact | `pip install ./artifact/*.whl` |
| **pip-index** (path B) | nightly wheel | `pytorch-ref` (wheel index `https://download.pytorch.org/whl/nightly/torch/`) | `pytorch-ref` passed verbatim (typically `nightly`); `actions/checkout@v7` resolves it | Wheel URL grepped from the index by checked-out commit's `devYYYYMMDD` + matrix `cu<label>` / `cp<pyshort>` tags |

In both paths the test job records the actual `git rev-parse HEAD` + commit date
of the checkout into its Step Summary, so each cell logs "what nightly did I
test" without needing a centralized preflight. This keeps `_rtx-build.yml` as
the only producer that needs to ship a wheel through GitHub artifact storage.
The nightly path avoids the fetch/upload/download round-trip entirely - the test
runner that resolves the ref is the same runner that pip-installs and tests.

The path-B resolver fails fast if the index has no wheel for the checked-out
commit's date - that is the signal that the nightly wheel for the source we just
pulled is not yet published, and any install would otherwise silently fall back
to an older wheel that disagrees with the source tree on disk.

## Default matrix

`config` (paired entries — each one corresponds to a real allocated runner):

| python | cuda toolkit | python-label | cuda-label |
| --- | --- | --- | --- |
| 3.12 | 13.0 | `py312` | `cu130` |
| 3.12 | 13.2 | `py312` | `cu132` |
| 3.13 | 13.2 | `py313` | `cu132` |

Plus `arch: [sm89, sm120]` on the orchestrator's test job, with the 5-shard
fanout living inside `_rtx-test.yml` (`strategy.matrix.shard: [1, 2, 3, 4, 5]`,
`NUM_TEST_SHARDS: "5"`), matching PR #176678.

Per source-build run that's **3 build jobs + 6 orchestrator-level test cells**
(3 configs x 2 archs); each test cell expands to 5 nested shard runners, so the
actual runner count is `3 + 6 * 5 = 33` GH Actions runner jobs. The wheel-test
run is **6 orchestrator-level test cells** (30 runners after the internal shard
fanout) - no preflight, no per-cell wheel producer.

`TORCH_CUDA_ARCH_LIST` is set per `arch` matrix entry (`8.9` for sm89, `12.0` for
sm120), and `runner-base` likewise (`rtx-40x0-test` vs `rtx-50x0-test`). The
build wheel itself is multi-arch (`8.9;12.0`) so a single producer feeds both
architectures.

## Test environment variables

`_rtx-test.yml` exports the subset of PR #176678's test env block that applies
off the pytorch-internal infra (no AWS, no `filter-test-configs`, no
`get-workflow-job-id`):

| Scope | Variable | Source |
| --- | --- | --- |
| job  | `BUILD_ENVIRONMENT`, `PYTHON_VERSION`, `CUDA_VERSION`, `TORCH_CUDA_ARCH_LIST` | matrix cell |
| job  | `USE_CUDA=1`, `INSTALL_WINDOWS_SDK=0`, `CONTINUE_THROUGH_ERROR=1`, `PYTORCH_TEST_WITH_SLOW=0`, `CI=1` | static |
| job  | `VC_PRODUCT=BuildTools`, `VC_YEAR=2022`, `VS_VERSION=17.4.1`, `VC_VERSION=""` | MSVC tooling info |
| step | `SHARD_NUMBER` | `_rtx-test.yml`'s internal `matrix.shard` |
| step | `NUM_TEST_SHARDS` | static (`"5"`, matches the shard list length) |
| step | `TEST_CONFIG` | `inputs.test-config` (default `"default"`) |
| step | `PYTORCH_FINAL_PACKAGE_DIR` | `${{ github.workspace }}/artifact` |
| step | `PR_NUMBER`, `SHA1` | `repository_dispatch` payload or PR context |
| step | `GITHUB_REPOSITORY` / `_WORKFLOW` / `_JOB` / `_RUN_ID` / `_RUN_NUMBER` / `_RUN_ATTEMPT` | `github.*` context |

## Test sharding

`test/run_test.py` assigns test files to the 5 shards itself, using per-file
timings it reads from `<pytorch>/.additional_ci_files/test-times.json`. Upstream
that file is downloaded from test-infra, which has no data for an out-of-tree
build env — hence the benign warning every shard logs:

```
Gathered no stats from artifacts for win-rtx-sm89 build env and default
test config. Using default job name and default test config instead.
```

The fallback to `default`/`default` is the intended path here: the
`Seed test-time stats` step runs `scripts/test-stats/seed_test_stats.py`, which
copies our committed `scripts/test-stats/data/*.json` into that folder under
exactly those keys. Every shard reads the same committed JSON, so all 5 agree on
the split without coordinating.

Timings matter more than they look. `calculate_shards` bin-packs by cost only
when a file's time is known; for an unknown file it falls back to round-robin
(`_get_min_sharded_job` in `tools/testing/test_selections.py`), which ignores
cost entirely. It also splits any file over a 10-minute `THRESHOLD` into
`ceil(duration / 600)` pytest shards spread across jobs — so with times,
`test_meta` becomes 13 pieces of ~9.4 min instead of one atomic 2-hour file that
pins whichever shard draws it.

### A missing time also removes the timeout

Balance is the visible effect; the timeout is the dangerous one. `run_test.py`
runs each test file in a subprocess and arms that subprocess's timeout only when
it knows how long the file should take:

```python
timeout = (... THRESHOLD * timeout_multiplier
           if should_retry
           and isinstance(test_module, ShardedTest)
           and test_module.time is not None
           else ... None)
```

`test_module.time` comes from `test-times.json` via
`test_selections.get_duration`, which returns `None` for a file it has never
seen. So **a file absent from the stats runs with no timeout at all.**

That is what cost this CI seven shards across four runs. In each case one CUDA
test deadlocked — `TestMemPool::test_graph_capture_pre_capture_stream_use` in
`test_cuda`, `_foreach_minimum` and `max_unpool1d` in `test_meta` — its file
never returned, the parent blocked in `pool.join()`, and the orphaned process
tree held the test step's stdout pipe open so the step could not end. GitHub
cancelled 70–120 minutes later. The runs recorded no failing test and no stack,
because the parent waits in `Process.join()` → `WaitForSingleObject(INFINITE)`,
which on Windows does not respond to Ctrl-C; only the idle pool workers ever
printed a traceback.

Three bounds now apply, outermost last:

| Bound | Where | Value |
| --- | --- | --- |
| per test | `PYTEST_ADDOPTS=--timeout=... --timeout-method=thread` | 15 min, dumps every thread's stack |
| per test file | `run_test.py` subprocess timeout (`THRESHOLD * 3`), armed by the seeded times | 30 min, exit 124, names the test in flight |
| per shard | in-step watchdog (`RUN_TEST_TIMEOUT_SEC`) | 195 min, kills `python.exe` so the step fails cleanly |

Because the per-file bound depends on the stats being complete,
`seed_test_stats.py` backfills an entry for every `test_*.py` in the checkout
that our data has never measured, using the median of the times we do have. The
value only has to be non-`None` to arm the timeout; the median keeps the guess
neutral for packing. The step prints how many it invented — `backfilled N
unmeasured file(s)` — and a steadily climbing `N` is the cue to regenerate.
`--no-backfill` restores the old, unbounded behaviour.

### Refreshing the stats

The data is a snapshot and drifts as the upstream suite changes. Drift now costs
accuracy rather than safety: an unmeasured file is backfilled, so it is packed
from a guess but still bounded. To regenerate from a completed run:

1. Download each shard's log for one `(config, arch)` cell — either
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

Each test job spawns `scripts/runner-diagnostics/monitor.ps1` (resolved by
`start-runner-diagnostics` from `$GITHUB_ACTION_PATH`) in the background while
`.ci/pytorch/win-test.sh` runs in the foreground. It writes one artifact per
shard, `runner-diagnostics-<env>-<py>-<cu>-shard<N>-<run_id>-<attempt>`, 14-day
retention, containing:

```
spec-snapshot.json   host / CPU / RAM / disk / driver / GPU / Python / nvcc
system.jsonl         CPU %, mem MiB, disk free / used GB, top 5 procs by WS
gpu.jsonl            per-GPU util, mem, temp, power, SM / mem clocks
monitor.log          start / stop bookends + sample count
```

Pipe the JSONL files through `jq` / `pandas` to plot pressure around a failure.

The test-job upload is deliberately on even though the build job's is off. When a
shard stalls, these samples are what separate "one test deadlocked on a healthy
box" from "the box was thrashing" — and the two need different fixes. Diagnosing
the seven lost shards above had to proceed without them.

## RFC-0050 mapping

| RFC concept | This repo |
| --- | --- |
| Downstream CI on real PR-time events | `windows-rtx-build-test.yml` (the `pytorch-pr-trigger` `repository_dispatch` arm is parked: its trigger is disabled in `on:` and the dispatch-gated jobs stay dormant) |
| `concurrency: upstream-pr-<pr_number>` | `windows-rtx-build-test.yml` keys `concurrency.group` on `client_payload.pr_number` when present |
| `pytorch/actions/checkout-pr@v1` (RFC Action #1) | Used as-is in `_rtx-build.yml` for `repository_dispatch`; falls back to `actions/checkout@v7` against `pytorch/pytorch@<ref>` for scheduled runs |

## Repository layout

```
.github/
  workflows/
    windows-rtx-wheel-test.yml           # nightly published-wheel smoke
    windows-rtx-build-test.yml            # full source build + test (scheduled; parked PR path)
    _rtx-build.yml                   # reusable: build source (.ci/pytorch/win-build.sh), uploads wheel artifact
    _rtx-test.yml                    # reusable: test a wheel (artifact OR pip-index install path)
  actions/
    start-runner-diagnostics/
      action.yml                     # composite: spawn monitor.ps1 in background
    stop-runner-diagnostics/
      action.yml                     # composite: signal stop, flush, summarise
scripts/
  runner-diagnostics/
    monitor.ps1                      # background sampler (host + GPU JSONL)
  test-stats/
    seed_test_stats.py               # copy data/*.json into <pytorch>/.additional_ci_files
    gen_test_stats.py                # rebuild data/*.json from a completed run's logs/reports
    data/
      test-times.json                # per-file seconds -> drives shard bin-packing
      test-class-times.json          # per-class seconds (partial-file TestRuns only)
```
