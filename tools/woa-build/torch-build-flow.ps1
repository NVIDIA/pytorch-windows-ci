#Requires -Version 5.1
<#
.SYNOPSIS
  GitHub Actions entrypoint for the WoA torch wheel build (vanilla + cuda_embed).

.DESCRIPTION
  Thin adapter around the vendored, env-driven flow
  `pytorch-windows-build-flow.ps1`. It translates the workflow's explicit
  parameters into the PYTORCH_WIN_BUILD_* / CHECKOUT_ROOT / CI_PROJECT_DIR env
  contract the flow expects, runs it in-process, then copies the distributable
  cuda_embed torch wheel into a flat -OutputDir for `actions/upload-artifact`.

  All the site-specific toolchain paths come from shared/env/defaults/*.psd1
  (WoA §10); this only wires the per-job dynamic values (checkout root, venv,
  arch list, CUDA/cuDNN overrides forwarded from the workflow WOA_* env).

  The dated wheel tree and its logs/WHEEL_OUT_ROOT marker are left in place so the
  torchaudio / torchvision entrypoints (later steps in the same job, sharing
  CI_PROJECT_DIR) can locate the cuda_embed torch wheel to build against.

.NOTES
  Exit code: 0 on success, non-zero on build failure (via Resolve-BuildFlowExitCode).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $PytorchRoot,
    [Parameter(Mandatory)][string] $OutputDir,
    [string] $VenvActivate
)

$ErrorActionPreference = 'Stop'

# --- env contract for the vendored flow -------------------------------------
# CI_PROJECT_DIR: where the flow writes logs/ + the WHEEL_OUT_ROOT marker that
# the extension steps read. Stable within a GitHub job -> use the workspace.
if ([string]::IsNullOrWhiteSpace($env:CI_PROJECT_DIR)) {
    $env:CI_PROJECT_DIR = if ($env:GITHUB_WORKSPACE) { $env:GITHUB_WORKSPACE } else { (Get-Location).Path }
}
$env:CHECKOUT_ROOT = $PytorchRoot

if (-not [string]::IsNullOrWhiteSpace($VenvActivate)) {
    $env:PYTORCH_WIN_BUILD_VENV_ACTIVATE = $VenvActivate
}

# Forward the workflow's WOA_* toolchain env (if set) onto the names the library
# reads. Unset ones fall through to the WoA §10 defaults in build-toolchain.psd1.
if ($env:TORCH_CUDA_ARCH_LIST) { $env:PYTORCH_WIN_BUILD_TORCH_CUDA_ARCH_LIST = $env:TORCH_CUDA_ARCH_LIST }
if ($env:WOA_CUDA_PATH)        { $env:PYTORCH_WIN_BUILD_CUDA_PATH   = $env:WOA_CUDA_PATH }
if ($env:WOA_CUDNN_ROOT)       { $env:PYTORCH_WIN_BUILD_CUDNN_ROOT  = $env:WOA_CUDNN_ROOT }
# Keep the dated wheel tree on job scratch (C:) unless the workflow overrode it.
if ([string]::IsNullOrWhiteSpace($env:PYTORCH_WIN_BUILD_WHEEL_OUT_DIR) -and $env:WOA_SCRATCH) {
    $env:PYTORCH_WIN_BUILD_WHEEL_OUT_DIR = (Join-Path $env:WOA_SCRATCH 'wheels')
}

# --- run the vendored flow in-process ---------------------------------------
. (Join-Path $PSScriptRoot 'pytorch-windows-build-flow.ps1')
$rc = Resolve-BuildFlowExitCode (Invoke-PytorchWindowsBuildFlow)
if ($rc -ne 0) {
    Write-Host "::error title=woa torch build::pytorch-windows-build-flow returned $rc"
    exit $rc
}

# --- collect the distributable cuda_embed torch wheel into OutputDir ---------
# Prefer the cuda_embed wheel (DLLs embedded); it shares the vanilla wheel's
# filename, so copying only this one avoids a flat-dir collision.
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$marker = Join-Path (Join-Path $env:CI_PROJECT_DIR 'logs') 'WHEEL_OUT_ROOT'
if (-not (Test-Path -LiteralPath $marker)) {
    Write-Host "::error title=woa torch build::WHEEL_OUT_ROOT marker not found at $marker"
    exit 1
}
$wheelRoot = (Get-Content -LiteralPath $marker -Raw).Trim()
$embedDir  = Join-Path $wheelRoot 'cuda_embed_dlls'
$torch = Get-ChildItem -LiteralPath $embedDir -Filter 'torch-*.whl' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $torch) {
    Write-Host "::error title=woa torch build::no cuda_embed torch wheel under $embedDir"
    exit 1
}
Copy-Item -LiteralPath $torch.FullName -Destination $OutputDir -Force
Write-Host "Staged cuda_embed torch wheel -> $OutputDir\$($torch.Name)"
exit 0
