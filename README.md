# pytorch-oot-internal

Internal staging ground for the NVIDIA PyTorch out-of-tree (OOT) CI on
self-hosted **Windows + NVIDIA RTX** runners. The workflows here implement
the downstream half of [RFC-0050: Cross-Repository CI Relay for PyTorch
Out-of-Tree Backends](https://github.com/pytorch/rfcs/blob/master/RFC-0050-Cross-Repository-CI-Relay-for-PyTorch-Out-of-Tree-Backends.md):
they consume `repository_dispatch` events of type `pytorch-pr-trigger` (sent
by the upstream Relay Server) and also run on a daily schedule against
`pytorch/pytorch` nightly so we have signal independent of the Relay Server.

The workflow shape is inspired by `pytorch/pytorch` PR
[#176678 - \[CI\]\[Windows\] Add NVIDIA RTX workflow](https://github.com/pytorch/pytorch/pull/176678).
Upstream covers a single configuration (Python 3.12, CUDA 12.8); this
internal repo deliberately expands the matrix so we can catch regressions
across multiple Python and CUDA toolkit combinations before they show up
upstream. Build/test logic itself comes entirely from PyTorch's in-tree
`.ci/pytorch/*.sh` scripts; this repo holds only the workflow wiring.

**Every job runs on a self-hosted runner provided by NVIDIA infrastructure.**
There are no GitHub-hosted (cloud) runs anywhere in this repo, including
the PR-time YAML validation.

## Topology

```
build  matrix( python x cuda )              one multi-arch wheel per cell
    |   each cell records its built SHA into the wheel artifact
    |
    +---> rtx-40x0-test  matrix( python x cuda )   sm89  / Ada
    +---> rtx-50x0-test  matrix( python x cuda )   sm120 / Blackwell
```

Build/test consistency is preserved without a central preflight job: each
build cell writes the exact pytorch SHA it compiled into
`built_pytorch_sha.txt`, ships it inside the wheel artifact, and the
matching test cell checks pytorch out at that SHA before installing.

## Default matrix

| python | cuda toolkit | python-label | cuda-label |
| --- | --- | --- | --- |
| 3.12 | 13.0 | `py312` | `cu130` |
| 3.12 | 13.2 | `py312` | `cu132` |
| 3.13 | 13.0 | `py313` | `cu130` |
| 3.13 | 13.2 | `py313` | `cu132` |

Per nightly run that's 4 build jobs + 8 test jobs (4 cells x 2 RTX architectures) = 12 jobs.

## Runner model

Runners are **ephemeral, pre-prepped images**. The image has the right
Python, CUDA toolkit, MSVC build tools, sccache, magma, cmake, ninja, and
the standard PyTorch test runtime pre-installed and on `PATH`. The
workflows perform **zero** in-job environment setup. Each matrix cell is
routed to its image via the runner-label set:

| Job kind | Label set |
| --- | --- |
| `build`         | `[self-hosted, Windows, X64, rtx-build, <python-label>, <cuda-label>]` |
| `rtx-40x0-test` (sm89  / Ada)        | `[self-hosted, Windows, X64, rtx-40x0-test, <python-label>, <cuda-label>]` |
| `rtx-50x0-test` (sm120 / Blackwell)  | `[self-hosted, Windows, X64, rtx-50x0-test, <python-label>, <cuda-label>]` |
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

This repo does **not** ship any helper scripts of its own - PyTorch's
in-tree CI scripts cover build, install, and test end-to-end.

## Workflows

```
.github/workflows/
  pytorch-nightly-rtx.yml    # top-level orchestrator (matrix build + matrix tests)
  _rtx-build.yml             # reusable build (invokes .ci/pytorch/win-build.sh)
  _rtx-test.yml              # reusable test  (invokes .ci/pytorch/win-test.sh)
  internal-validation.yml    # PR-time YAML lint (self-hosted)
```

`pytorch-nightly-rtx.yml` triggers:

| Trigger | Behavior |
| --- | --- |
| `schedule` (`0 12 * * *`) | Build + test against `pytorch/pytorch@nightly` |
| `workflow_dispatch` | Inputs: `pytorch-ref` (default `nightly`), `test-architectures` (default `sm89,sm120`) |
| `repository_dispatch: [pytorch-pr-trigger]` | RFC-0050 path: build cells use `pytorch/actions/checkout-pr@v1` with `client_payload.head_sha` |

## Customising the matrix

To add or remove cells edit the three `strategy.matrix` blocks in
`pytorch-nightly-rtx.yml` (build, rtx-40x0-test, rtx-50x0-test). Keep the
three matrices in sync - the test jobs construct each cell's wheel artifact
name deterministically from `python-label` + `cuda-label`, so a test cell
without a matching build cell will fail to download its wheel.

## RFC-0050 mapping

| RFC concept | This repo |
| --- | --- |
| Downstream repo receiving `repository_dispatch:[pytorch-pr-trigger]` | `pytorch-nightly-rtx.yml` |
| `concurrency: upstream-pr-<pr_number>` | `concurrency.group` uses `client_payload.pr_number` when present |
| `pytorch/actions/checkout-pr@v1` (RFC Action #1) | used as-is in `_rtx-build.yml` for `repository_dispatch`; falls back to `actions/checkout@v4` against `pytorch/pytorch@<ref>` for schedule / manual runs |
| `pytorch/actions/report-ci-result@v1` (RFC Action #2) | not yet wired (Relay Server is not live); we'll add the upstream action once it's published |

## Local workflow validation

```bash
python -m pip install "PyYAML>=6" "check-jsonschema>=0.29"
check-jsonschema --builtin-schema vendor.github-workflows .github/workflows/*.yml
```

The same checks run automatically in `internal-validation.yml` on every PR.
