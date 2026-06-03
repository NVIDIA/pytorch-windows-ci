<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: MIT
-->

# pytorch-oot-internal

Internal staging ground for the NVIDIA PyTorch out-of-tree (OOT) CI on
self-hosted **Windows + NVIDIA RTX** runners. The workflows here implement
the downstream half of [RFC-0050: Cross-Repository CI Relay for PyTorch
Out-of-Tree Backends](https://github.com/pytorch/rfcs/blob/master/RFC-0050-Cross-Repository-CI-Relay-for-PyTorch-Out-of-Tree-Backends.md)
and mirror the in-tree shape of `pytorch/pytorch` PR
[#176678 - \[CI\]\[Windows\] Add NVIDIA RTX workflow](https://github.com/pytorch/pytorch/pull/176678).
Upstream covers a single configuration (Python 3.12, CUDA 12.8); this
internal repo deliberately expands the matrix so we can catch regressions
across multiple Python and CUDA toolkit combinations before they show up
upstream. Build/test logic itself comes entirely from PyTorch's in-tree
`.ci/pytorch/*.sh` scripts; this repo holds only the workflow wiring.

**Every job runs on a self-hosted runner provided by NVIDIA infrastructure.**
There are no GitHub-hosted (cloud) runs anywhere in this repo, including
the PR-time YAML validation.

## License and notices

This repository is released under MIT terms. See [LICENSE](LICENSE) for
the project license, [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for
third-party OSS notices.

## Three top-level workflows

| Workflow | Purpose | Triggers | Compute |
| --- | --- | --- | --- |
| `relay-server-event-validate.yml`  | RFC-0050 handshake. Confirms relay events arrive, validates payload, will round-trip an ack via `report-ci-result@v1` once published. | `repository_dispatch:[pytorch-pr-trigger, pytorch-ping]`, `workflow_dispatch` | One lightweight self-hosted runner; no pytorch checkout, no build, no install. |
| `nightly-wheel-test.yml`           | Each test cell checks out `pytorch/pytorch` at `pytorch-ref` (default `nightly`) via `actions/checkout@v4` (which resolves the branch to a concrete commit), records the actual HEAD SHA + commit date into the cell's job summary, then greps `download.pytorch.org/whl/nightly/torch/` for the wheel whose filename carries that exact `devYYYYMMDD` tag together with the matrix `cu<label>` / `cp<pyshort>` tags and `pip install`s the resolved absolute URL before running `.ci/pytorch/win-test.sh`. Fails fast if no matching wheel exists, so the wheel under test always shares its commit date with the pytorch source on disk. No preflight job, no artifact transit. | `schedule` (`0 14 * * *`), `workflow_dispatch` | `_rtx-test.yml` (sm89 + sm120 in one matrix) |
| `nightly-source-build-test.yml`    | Full source build (multi-arch wheel) + test. The path that handles real RFC-0050 PR-time events. | `schedule` (`0 12 * * *`), `workflow_dispatch`, `repository_dispatch:[pytorch-pr-trigger]` | `_rtx-build.yml` -> `_rtx-test.yml` (sm89 + sm120 in one matrix) |

Both nightly workflows fan out across `(config)` for builds and
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
also lines up a wheel-test row alongside its source-build-test
counterpart in cross-workflow dashboards.

`.ci/pytorch/win-test.sh` (via `test/run_test.py`) honours the
`SHARD_NUMBER` / `NUM_TEST_SHARDS` / `TEST_CONFIG` env vars set
inside `_rtx-test.yml` to run just its slice.

```
nightly-source-build-test.yml:                  nightly-wheel-test.yml:

  build  matrix( config )                         (no preflight job)
      |   (4 cells)                                test  matrix( config x arch )
      |   multi-arch wheel + SHA sidecar                  (4 x 2 = 8 cells)
      |   (artifact upload currently disabled)
      |                                                  each cell calls
      +-> test  matrix( config x arch )                  _rtx-test.yml, which
                (4 x 2 = 8 cells)                        internally fans out
                  each cell calls _rtx-test.yml,         5 shard runners.
                  which internally fans out 5
                  shard runners (40 runners total).      Inside each runner:
                                                           - checkout pytorch@nightly
                  Inside each runner:                      - grep public index for the
                    - pip install the build's wheel          matching devYYYYMMDD wheel
                      artifact                             - pip install URL
                    - run shard N of 5                     - run shard N of 5

UI grouping in both workflows (orchestrator level):
  wheel-py312-cu130-build                  (source-build-test only)
  wheel-py312-cu130-sm89-test              ... drill in for 5 shard cells
  wheel-py312-cu130-sm120-test             ... drill in for 5 shard cells
  wheel-py312-cu132-build                  (source-build-test only)
  wheel-py312-cu132-sm89-test
  ...
```

`_rtx-test.yml` accepts two install paths and routes between them based
on which inputs the orchestrator provided:

| Install path | When | Required inputs | Checkout ref from | Install source |
| --- | --- | --- | --- | --- |
| **artifact** (path A) | source build | `wheel-artifact` | SHA in `built_pytorch_sha.txt` inside the artifact | `pip install ./artifact/*.whl` |
| **pip-index** (path B) | nightly wheel | `pytorch-ref` (+ optional `wheel-index-url`, default `https://download.pytorch.org/whl/nightly/torch/`) | `pytorch-ref` passed verbatim (typically `nightly`); `actions/checkout@v4` resolves it | Wheel URL grepped from the index by checked-out commit's `devYYYYMMDD` + matrix `cu<label>` / `cp<pyshort>` tags |

In both paths the test job records the actual `git rev-parse HEAD` +
commit date of the checkout into its Step Summary, so each cell logs
"what nightly did I test" without needing a centralized preflight.
This keeps `_rtx-build.yml` as the only producer that needs to ship a
wheel through GitHub artifact storage. The nightly path avoids the
fetch/upload/download round-trip entirely - the test runner that
resolves the ref is the same runner that pip-installs and tests.

The path-B resolver fails fast if the index has no wheel for the
checked-out commit's date - that is the signal that the nightly wheel
for the source we just pulled is not yet published, and any install
would otherwise silently fall back to an older wheel that disagrees
with the source tree on disk.

## Default matrix

`config` (paired entries — each one corresponds to a real allocated
runner; add/remove entries to match the runner pool):

| python | cuda toolkit | python-label | cuda-label |
| --- | --- | --- | --- |
| 3.12 | 13.0 | `py312` | `cu130` |
| 3.12 | 13.2 | `py312` | `cu132` |
| 3.13 | 13.0 | `py313` | `cu130` |
| 3.13 | 13.2 | `py313` | `cu132` |

Plus `arch: [sm89, sm120]` on the orchestrator's test job, with the
5-shard fanout living inside `_rtx-test.yml`
(`strategy.matrix.shard: [1, 2, 3, 4, 5]`, `NUM_TEST_SHARDS: "5"`),
matching PR #176678.

Per source-build run that's **4 build jobs + 8 orchestrator-level
test cells** (4 configs x 2 archs); each test cell expands to 5
nested shard runners, so the actual runner count is `4 + 8 * 5 = 44`
GH Actions runner jobs. The nightly-wheel run is **8 orchestrator-
level test cells** (40 runners after the internal shard fanout) - no
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
| `relay-server-event-validate`                   | `[rtx-build]` (any free build runner) |
| `internal-validation` (PR-time YAML lint)       | `[rtx-build]` (any free build runner) |

The narrow labels (`rtx-build`, `rtx-40x0-test`, `rtx-50x0-test`,
`py3xx`, `cu1xx`) are unique to our self-hosted Windows pool, so the
GitHub auto-tags (`self-hosted`, `Windows`, `X64`) that the runner
agent applies are redundant in the AND filter and are deliberately
left off `runs-on:` everywhere.

For example, the sm120 test cell for Python 3.13 + CUDA 13.0 needs an
image registered as `[rtx-50x0-test, py313, cu130]` (plus whatever
auto-tags the runner agent adds).

## What the runner image must already contain

Because we do no in-job setup, the pre-prepped image carries everything the
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
  for the runner-diagnostics composite actions and the relay-validate
  workflow. PowerShell 7+ (`pwsh`) is NOT required - every script in
  this repo sticks to cmdlets and language features available in 5.1.

The only repo-local helper is the runner-diagnostics monitor described
[below](#runner-diagnostics); PyTorch's in-tree CI scripts cover build,
install, and test end-to-end.

## File layout

```
.github/
  workflows/
    relay-server-event-validate.yml  # RFC-0050 handshake (no compute)
    nightly-wheel-test.yml           # nightly published-wheel smoke
    nightly-source-build-test.yml    # full source build + test (PR-trigger path)
    _rtx-build.yml                   # reusable: build source (.ci/pytorch/win-build.sh), uploads wheel artifact
    _rtx-test.yml                    # reusable: test a wheel (artifact OR pip-index install path)
    internal-validation.yml          # PR-time YAML lint (self-hosted)
  actions/
    start-runner-diagnostics/
      action.yml                     # composite: spawn monitor.ps1 in background
    stop-runner-diagnostics/
      action.yml                     # composite: signal stop, flush, summarise
scripts/
  runner-diagnostics/
    monitor.ps1                      # background sampler (host + GPU JSONL)
```

## Customising the matrix

Each orchestrator has at most two jobs - `build` and `test`. The
orchestrator's test matrix is 2-dimensional (`config x arch`); the
shard fanout lives one layer down in `_rtx-test.yml`:

```yaml
# Orchestrator (nightly-source-build-test.yml / nightly-wheel-test.yml)
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

In `nightly-source-build-test.yml`, the `config` list is declared on
the `build` job (`&config` anchor) and re-used on the `test` job
(`*config`). In `nightly-wheel-test.yml` the list lives directly on
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

## Test env vars

`_rtx-test.yml` exports the subset of PR #176678's test env block that
applies off the pytorch-internal infra (no AWS, no `filter-test-configs`,
no `get-workflow-job-id`):

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

## RFC-0050 mapping

| RFC concept | This repo |
| --- | --- |
| Handshake workflow that proves the downstream is wired | `relay-server-event-validate.yml` (listens for `pytorch-pr-trigger` and the heartbeat-only `pytorch-ping`) |
| Downstream CI on real PR-time events | `nightly-source-build-test.yml` (also subscribes to `pytorch-pr-trigger`) |
| `concurrency: upstream-pr-<pr_number>` | Both workflows above key `concurrency.group` on `client_payload.pr_number` when present |
| `pytorch/actions/checkout-pr@v1` (RFC Action #1) | Used as-is in `_rtx-build.yml` for `repository_dispatch`; falls back to `actions/checkout@v4` against `pytorch/pytorch@<ref>` for schedule / manual runs |
| `pytorch/actions/report-ci-result@v1` (RFC Action #2) | Stubbed in `relay-server-event-validate.yml` (acknowledgement step is a placeholder); we will swap in the upstream action once published |

## Local workflow validation

```bash
python -m pip install "PyYAML>=6" "check-jsonschema>=0.29"
check-jsonschema --builtin-schema vendor.github-workflows .github/workflows/*.yml
```

The same checks run automatically in `internal-validation.yml` on every PR.
