<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: MIT -->

# Windows-on-Arm (WoA) PyTorch CI — operator guide

Out-of-tree CI that builds and tests **PyTorch on Windows arm64** from source, the
arm64 counterpart of the x86 `windows-rtx-build-test` flow. This is the
operator-facing guide: how to run it, what the runners must provide, and how to
read the results. For the design and the locked decisions see
[`woa-ci-plan.md`](./woa-ci-plan.md); for the PowerShell library internals see
[`tools/woa-build/README.md`](../tools/woa-build/README.md).

## Job graph

```
prep (resolve pytorch ref)                        ubuntu-latest
  └─ build  matrix(py311,py312,py313,py314,py314t) woa-arm64   ── each cell:
  │         vanilla → cuda_embed → torchaudio → torchvision → upload wheel artifact
  └─ test   matrix(same cells)   [needs: build]    woa-arm64   ── each fans out N shards
       └─ test-summary  (if: always())             ubuntu-latest
```

- **`windows-woa-build-test.yml`** — the orchestrator (nightly schedule).
- **`_woa-build.yml`** — reusable build workflow (one call per Python cell).
- **`_woa-test.yml`** — reusable test workflow (one call per Python cell, internal shard matrix).
- `py314t` is the free-threaded (no-GIL) 3.14 build and produces `cp314t` wheels.
- `test` `needs: build`, so the whole build stage finishes before any test job
  starts — this also stops test jobs from starving the build stage on the shared
  runner pool.

## Running it

**`windows-woa-build-test`** runs automatically on a **nightly schedule**
(`cron: 0 5 * * *`) — there is **no** manual `workflow_dispatch` trigger. Every
run builds and tests the **full** matrix with a fixed config:

| Setting | Value | Meaning |
| --- | --- | --- |
| pytorch ref | `nightly` | `pytorch/pytorch` ref built + tested, resolved to a full commit SHA in `prep`. |
| python versions | `3.13` (temporary) | Currently limited to **3.13 only**; the other cells (`3.11,3.12,3.14,3.14t`) are commented out in the orchestrator's matrix and will be re-enabled shortly (`3.14t` = free-threaded). |
| shards | `4` | Test shard count; **must match** the `shard:` matrix length in `_woa-test.yml` (fixed for now; see plan section 4). |

The `prep` job prints the resolved config to the run summary. Each build cell
records the concrete pytorch commit into `built_pytorch_sha.txt` inside its wheel
artifact, and the test shards check out **exactly that SHA**, so build and test
always agree on the source even when `nightly` moves mid-run.

### Source integrity (SHA pinning + HTTPS/TLS)

The pytorch source acquisition is pinned and encrypted end to end:

- **SHA pinning.** `prep` resolves the `nightly` ref to a
  **full 40-hex commit SHA** over HTTPS (`git ls-remote`) and passes that SHA to
  every build cell, so all cells build the identical commit even if `nightly`
  advances mid-run. `_woa-build.yml` additionally **rejects** any `pytorch-ref`
  that is not a 40-hex SHA (enforced even for direct reusable-workflow callers),
  and the test cells re-pin to the 40-hex `built_pytorch_sha.txt`. Because Git
  objects are content-addressed, a full-SHA checkout yields exactly that commit
  or fails.
- **HTTPS/TLS.** All checkouts use `actions/checkout` over `https://github.com`
  (TLS); there is no SSH path and credentials are not persisted
  (`persist-credentials: false`). Submodules are fetched over the same transport.

### Changing the shard count

Edit **both** the `shard: [1,2,3,4]` matrix **and** the `num-shards` input
default in `_woa-test.yml` (they must stay equal). Variable-by-runner sharding
is deferred.

## Runner requirements (`woa-arm64` pool)

There is no runner image — every runner in the shared, persistent `woa-arm64`
pool must be provisioned to the contract in [plan section 10](./woa-ci-plan.md#10-runner-preinstall-contract-paths-the-workflows-assume).
Summary:

- **PowerShell 7 (`pwsh`)** and `ninja` on `PATH`; `git` with long-path support.
- **Clean ARM64 CPython interpreters only** (`3.11`, `3.12`, `3.13`, `3.14` and the
  free-threaded `3.14t` via `python3.14t.exe`) installed `--architecture arm64` by
  the infra provisioner. **No pre-built venvs** — each job builds its own fresh,
  throwaway venv from the clean interpreter (`woa-create-venv`, under `C:\ci\woa\scratch\venv`,
  wiped by strict-clean), then installs a **role-scoped** dependency set from
  `tools/woa-build/shared/requirements/`:
  - **build** → `woa-base.txt` + `woa-build.txt` (strict; build toolchain only).
  - **test** → `woa-base.txt` + `woa-test.txt` (strict harness) + `woa-test-extended.txt`
    (best-effort domain/ONNX/solver stack — a package with no win_arm64 wheel is skipped).
  - `woa-base.txt` is shared: both roles need it to build sdists / JIT cpp-extension
    tests + the runtime. Nothing a job installs persists to the next job (public-facing
    runners). Rust/Cargo is preinstalled so Rust-sdist deps (`tlparse`, `lintrunner`)
    build when no win_arm64 wheel exists.
- **Toolchain (CTK 13.4):** CUDA `C:\Program Files\NVIDIA\CUDA\v13.4`, cuDNN
  `C:\Program Files\NVIDIA\CUDNN\v9.25`, MSVC arm64 `vcvarsall.bat`, APL
  (`C:\DevToolKit\APL`) + vcpkg libuv (`C:\DevToolKit\vcpkg`). These are the
  single-drive `C:` install paths every arm64 runner must provide.
- Build + test share the pool; the test runners additionally need a **GPU**
  (`woa-preflight-test` asserts `nvidia-smi`).

`woa-preflight-build` / `woa-preflight-test` verify these paths, a clean ARM64
interpreter for the cell's Python (an x64-only install fails here in seconds, not
after a 40-minute compile), and a GPU (test) at job start — **fail fast** on a
misprovisioned runner, the same-machine guarantee an in-job preflight provides.
If defaults don't match your provisioning, override via the
`WOA_*` env in the workflow or edit `tools/woa-build/shared/env/defaults/*.psd1`.

### Strict cleanup (security)

Because the runners are persistent and may be exposed to untrusted code, the
`woa-strict-clean` composite action scrubs job-produced state (the relocated
checkout `C:\pt`, build scratch including the per-job venv + pip cache, wheels,
downloaded artifacts) at **both** job start (`pre`) and end (`post`, under
`if: always()`). Preinstalled toolchains and the clean interpreters are never
touched. All checkouts use `persist-credentials: false`.

## Artifacts

| Artifact | Produced by | Contents |
| --- | --- | --- |
| `woa-<pylabel>-cu134-<run_id>` | each build cell | the `cuda_embed` torch wheel + torchaudio + torchvision wheels + `built_pytorch_sha.txt` |
| `test-reports-win-woa-arm64-<pylabel>-arm64-shard<N>-<run_id>-<attempt>` | each test shard | JUnit XML + `run_test.py` logs |
| `runner-diagnostics-woa-*` | build/test | runner diagnostics (best-effort) |

The distributable torch wheel is the `cuda_embed` one (CUDA DLLs embedded); the
test job installs it in preference to any vanilla wheel.

## Reading results

The **`test-summary`** job (always runs, never reports a status of its own) writes
two sections to the run summary:

1. **Failed test jobs** — which build/test jobs failed, with log links
   (`aggregate_failures.py`).
2. **Aggregate failed tests across shards** — the de-duplicated failing-test list,
   per-cell and overall, showing which shard(s) each failure came from
   (`parse_failures.py`). Each shard also prints its own summary in its job log.

**A failing test turns its shard red** (x86 / `_rtx-test.yml` parity): `run_test.py`
runs with `--keep-going` / `CONTINUE_THROUGH_ERROR=1`, so the shard runs to the end even
after a failure, then exits non-zero if anything failed — the job goes **red** and the
per-shard + aggregate summaries list the failing tests. There is no triage server — the
failing tests are listed in the summary here. Red shards are
**non-blocking**: `fail-fast: false` keeps sibling shards and other cells running.

**Extension builds are non-blocking (but shown red):** torchaudio / torchvision run
with `continue-on-error: true`, so a failure doesn't skip the sibling extension or the
torch-wheel upload. A final `Reflect extension build status` gate then fails the build
job if either extension failed — so the failure **shows as a red build job** (same
principle as a failing test shard) yet never blocks the torch wheel or the other python
cells (`fail-fast: false`). The missing extension wheel simply won't be in the artifact,
and the test job warns and continues.

## Troubleshooting

| Symptom | Likely cause / fix |
| --- | --- |
| `woa-preflight-build/test` fails immediately | Runner missing a section 10 path (CUDA/cuDNN), a clean ARM64 interpreter for the cell's Python, or a GPU. Re-provision or override the `WOA_*` env. |
| `woa-create-venv` fails | No ARM64 interpreter for the cell (or an x64 one shadows it), or a **strict core** package failed to install. Extended (best-effort) failures only warn. Re-provision the arm64 CPython; check the pip log in the step. |
| `WHEEL_OUT_ROOT marker not found` in an extension step | The torch build step didn't complete in the same job, or `CI_PROJECT_DIR` differs between steps. Extensions read the marker the torch step writes. |
| Test shard fails with `TORCH_IMPORT_FAILED` | The installed wheel can't load torch (e.g. a missing embedded DLL → WinError 126). A synthetic failing JUnit is emitted so the shard stays visible in the summary. |
| Shard goes non-green with `WATCHDOG_TIMEOUT` | A test hung; `run_test.py` was killed after the wall-clock cap (`RunTestTimeoutSec`). The watchdog synthesizes a JUnit naming the closest test. |
| `Join-Path` / parser errors in a build/test step | The library needs `pwsh` 7 — confirm the step is `shell: pwsh` and `pwsh` is on the runner. |
| A test cell is skipped | Its build cell produced no wheel (the build failed for that Python). Every Python cell always runs — there is no version-subset input. |

## Development

- Library internals, entrypoints, and the env contract:
  [`tools/woa-build/README.md`](../tools/woa-build/README.md).
- CI-of-CI: `lint.yml` validates workflow YAML/schema and parse-checks +
  PSScriptAnalyzer-lints `tools/woa-build/**` on every PR that touches
  `.github/**` or `tools/woa-build/**`.
- Re-vendoring: keep the four GitHub entrypoints and the `.psd1` site defaults; do
  not add GitHub-specific behaviour to the `shared`/flow scripts.
