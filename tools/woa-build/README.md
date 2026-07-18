<!-- SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved. -->
<!-- SPDX-License-Identifier: MIT -->

# `tools/woa-build` — Windows-on-Arm (WoA) build + test library

PowerShell library that builds and tests **PyTorch on Windows arm64** from source
for this repo's out-of-tree CI. It is invoked by the WoA GitHub Actions workflows
(`.github/workflows/_woa-build.yml`, `_woa-test.yml`, driven by
`windows-woa-build-test.yml`) and produces the full P0 wheel set:

```
torch (vanilla) → torch (cuda_embed, DLLs embedded) → torchaudio → torchvision → test → summary
```

See [`docs/woa-ci-plan.md`](../../docs/woa-ci-plan.md) for the design, the locked
decisions, and the runner contract (§10).

## Overview

An env-driven PowerShell build/test library. The flow scripts and `shared/**`
helpers are generic; all GitHub adaptation lives in the entrypoints and the
site-default `.psd1` files (below).

## Entrypoints

The workflows call these; each is a thin adapter that sets the library's env
contract, runs the vendored flow in-process, and collects wheels into a flat
`-OutputDir`.

| Entrypoint | Purpose |
| --- | --- |
| `torch-build-flow.ps1` | Build torch vanilla + `cuda_embed`; stage the `cuda_embed` wheel (the distributable one). |
| `torchaudio/build-pipeline.ps1` | Build torchaudio against the `cuda_embed` torch wheel. |
| `torchvision/build-pipeline.ps1` | Build torchvision (vcpkg/codecs + delvewheel repair) against `cuda_embed` torch. |
| `pytorch-windows-test-shard.ps1` | Run one `run_test.py` shard under a wall-clock watchdog; leave JUnit under `test\test-reports`. |

Common parameters: `-PytorchRoot`, `-OutputDir`, `-VenvActivate` (build);
`-PytorchRoot`, `-ShardNumber`, `-NumShards`, `-TestConfig`, `-VenvActivate`
(test).

## Layout

```
torch-build-flow.ps1              # GitHub entrypoint: vanilla + cuda_embed
pytorch-windows-build-flow.ps1    # vendored flow (env-driven; called by the entrypoint)
pytorch-windows-test-shard.ps1    # GitHub test entrypoint (no UNC install / no publish)
torch/                            # WheelPipeline, CompilerAndBuildEnv, Common
torchaudio/  torchvision/         # build-pipeline.ps1 (entrypoint) + build-flow.ps1 (vendored) + Build.ps1
shared/
  env/      # Resolve-CiEnv / Set-CiEnv / Get-CiDefault + defaults/*.psd1 (site config)
  log/      # Write-CiPhase structured phase logging
  workflow/ # Prereqs, Subset, FlowExitCode, LoggedExec
  build/    # ImportVcvars, CudaDelveAddPath, ResolveTorchWheel, extension pipeline
  test/     # run_test.py shard runner, watchdog + synthetic-failure JUnit
  io/       # long-path-safe delete
```

## Env contract

The vendored flow is env-driven; the entrypoints translate workflow inputs onto
these names (and `Resolve-CiEnv` falls through to the `.psd1` defaults for the
rest):

| Env var | Set by | Meaning |
| --- | --- | --- |
| `CHECKOUT_ROOT` | entrypoint (`-PytorchRoot`) | relocated short-path pytorch checkout (`C:\pt`) |
| `CI_PROJECT_DIR` | entrypoint (`GITHUB_WORKSPACE`) | logs dir + `logs/WHEEL_OUT_ROOT` marker (shared build→ext steps) |
| `PYTORCH_WIN_BUILD_VENV_ACTIVATE` / `PYTORCH_WIN_TEST_VENV_ACTIVATE` | entrypoint (`-VenvActivate`) | per-Python venv activation script |
| `PYTORCH_WIN_BUILD_*` (CUDA/cuDNN/arch) | workflow `WOA_*` env → entrypoint, else `.psd1` | toolchain overrides |
| `PYTORCH_WIN_TEST_RUN_TEST_EXTRA_ARGS` | test entrypoint | `--exclude-jit-executor --keep-going --exclude-distributed-tests --exclude-quantization-tests --verbose` |

## Site defaults (`shared/env/defaults/*.psd1`)

The WoA §10 runner contract lives here (overridable via the `PYTORCH_WIN_*` env
vars above), matching the single-drive `C:` `pytorch-windows-infra` arm64
provisioner: CUDA `C:\Program Files\NVIDIA\CUDA\v13.4` + cuDNN
`C:\Program Files\NVIDIA\CUDNN\v9.25`, APL/libuv under `C:\DevToolKit`, the fresh
per-job venv under `C:\ci\woa\scratch\venv\<pylabel>` (built by `woa-create-venv`
from the runner's clean ARM64 interpreter — no preinstalled venvs),
wheel/scratch/ext-work roots under
`C:\ci\woa\scratch`, `TORCH_CUDA_ARCH_LIST = 8.9;10.3+PTX;12.0;12.1+PTX` (extensions
use the same suffix-free torch list plus `EXT_WIN_CMAKE_CUDA_ARCHITECTURES=89;103a;120f`).
Edit these to match
how the `woa-arm64` runners are provisioned.

## Dropped from the source CI

The source CI's toolkit auto-update, UNC-share publish/install, and the git-bash
wrappers are not carried here. Runner-worktree cleanup + preflight are
reimplemented as the composite actions `woa-strict-clean` / `woa-preflight-build`
/ `woa-preflight-test`. Wheel handoff is GitHub Actions artifacts; reporting is
summary-only (`scripts/test-summary/parse_failures.py`).

## Requirements & local checks

- **PowerShell 7 (`pwsh`)** on the runner — the library uses the pwsh-6+
  multi-argument `Join-Path`, so the library-invoking workflow steps run under
  `shell: pwsh`. `ninja` must be on `PATH`.
- Syntax-check every script (no `pwsh` needed):

  ```powershell
  Get-ChildItem -Recurse -Filter *.ps1 | ForEach-Object {
    $e = $null
    [System.Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$null, [ref]$e) | Out-Null
    if ($e) { "$($_.Name): $($e.Count) errors" }
  }
  ```

## Re-vendoring

When pulling in newer upstream scripts, re-apply the prune list above and keep the
four GitHub entrypoints and the `.psd1` site defaults. Do **not** edit the flow /
`shared` scripts for GitHub-specific behaviour — put it in the entrypoints or the
env contract so future updates stay clean.
