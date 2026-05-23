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

## Three top-level workflows

| Workflow | Purpose | Triggers | Compute |
| --- | --- | --- | --- |
| `relay-server-event-validate.yml`  | RFC-0050 handshake. Confirms relay events arrive, validates payload, will round-trip an ack via `report-ci-result@v1` once published. | `repository_dispatch:[pytorch-pr-trigger, pytorch-ping]`, `workflow_dispatch` | One lightweight self-hosted runner; no pytorch checkout, no build, no install. |
| `nightly-wheel-test.yml`           | Each test cell checks out `pytorch/pytorch` at `pytorch-ref` (default `nightly`) via `actions/checkout@v4` (which resolves the branch to a concrete commit), records the actual HEAD SHA + commit date into the cell's job summary, then greps `download.pytorch.org/whl/nightly/torch/` for the wheel whose filename carries that exact `devYYYYMMDD` tag together with the matrix `cu<label>` / `cp<pyshort>` tags and `pip install`s the resolved absolute URL before running `.ci/pytorch/win-test.sh`. Fails fast if no matching wheel exists, so the wheel under test always shares its commit date with the pytorch source on disk. No preflight job, no artifact transit. | `schedule` (`0 14 * * *`), `workflow_dispatch` | `_rtx-test.yml` (sm89 + sm120) |
| `nightly-source-build-test.yml`    | Full source build (multi-arch wheel) + test. The path that handles real RFC-0050 PR-time events. | `schedule` (`0 12 * * *`), `workflow_dispatch`, `repository_dispatch:[pytorch-pr-trigger]` | `_rtx-build.yml` -> `_rtx-test.yml` (sm89 + sm120) |

Both nightly workflows fan out across the same `(python x cuda)` matrix
(declared once via a YAML anchor on the first test job and re-used by
the second) and share `_rtx-test.yml` as the single test runner.

```
nightly-source-build-test.yml:                         nightly-wheel-test.yml:

  build  matrix( python x cuda )                         (no preflight job)
      |  multi-arch wheel + SHA sidecar per cell
      |  uploaded as artifact
      |                                                    rtx-40x0-test (sm89)   matrix( python x cuda )
      +--->  rtx-40x0-test (sm89)                          rtx-50x0-test (sm120)  matrix( python x cuda )
      +--->  rtx-50x0-test (sm120)                          (each cell: checkout pytorch@nightly,
                                                            grep public index for the matching
                                                            devYYYYMMDD wheel, pip install URL)
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

| python | cuda toolkit | python-label | cuda-label |
| --- | --- | --- | --- |
| 3.12 | 13.0 | `py312` | `cu130` |
| 3.12 | 13.2 | `py312` | `cu132` |
| 3.13 | 13.0 | `py313` | `cu130` |
| 3.13 | 13.2 | `py313` | `cu132` |

Per source-build run that's 4 build jobs + 8 test jobs (4 cells x 2
RTX architectures) = 12 jobs. The nightly-wheel run is 8 test jobs
only - no preflight, no per-cell wheel producer.

## Runner model

Runners are **ephemeral, pre-prepped images**. The image has the right
Python, CUDA toolkit, MSVC build tools, sccache, magma, cmake, ninja, and
the standard PyTorch test runtime pre-installed and on `PATH`. The
workflows perform **zero** in-job environment setup. Each matrix cell is
routed to its image via the runner-label set:

| Job kind | Label set |
| --- | --- |
| `build` (source-build wheel producer) | `[self-hosted, Windows, X64, rtx-build, <python-label>, <cuda-label>]` |
| `rtx-40x0-test` (sm89  / Ada)        | `[self-hosted, Windows, X64, rtx-40x0-test, <python-label>, <cuda-label>]` |
| `rtx-50x0-test` (sm120 / Blackwell)  | `[self-hosted, Windows, X64, rtx-50x0-test, <python-label>, <cuda-label>]` |
| `relay-server-event-validate`        | `[self-hosted, Windows, X64, rtx-build]` (any free build runner) |
| `internal-validation` (PR-time YAML lint) | `[self-hosted, Windows, X64, rtx-build]` (any free build runner) |

For example, the sm120 test cell for Python 3.13 + CUDA 13.0 needs an image registered as
`[self-hosted, Windows, X64, rtx-50x0-test, py313, cu130]`.

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
- `pwsh` (PowerShell 7+) on `PATH` - required by the runner-diagnostics
  composite actions

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

The `(python x cuda)` matrix is declared once on the first test job in
each orchestrator (`build` in `nightly-source-build-test.yml`,
`rtx-40x0-test` in `nightly-wheel-test.yml`) via a YAML anchor
(`matrix: &matrix`) and re-used on the second test job
(`matrix: *matrix`) so the fanouts within a workflow stay in lockstep.
Edit the anchor in the relevant orchestrator file to add or remove
cells.

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
