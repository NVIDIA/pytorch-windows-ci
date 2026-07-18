#Requires -Version 5.1
<#
.SYNOPSIS
  GitHub Actions entrypoint for the WoA torchaudio extension wheel build.

.DESCRIPTION
  Thin adapter around the vendored, env-driven flow `build-flow.ps1` in
  this directory. It sets the CI_PROJECT_DIR / venv env contract, runs the flow
  (which reads the logs/WHEEL_OUT_ROOT marker left by torch-build-flow.ps1,
  builds an isolated venv, installs the cuda_embed torch wheel, clones
  pytorch/audio, and pip-wheels torchaudio into cuda_embed_dlls/), then copies
  the produced torchaudio wheel into a flat -OutputDir.

  Extension builds are P0 but non-blocking at the workflow level
  (continue-on-error), matching the extension's continue-on-error policy.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $OutputDir,
    [string] $VenvActivate
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($env:CI_PROJECT_DIR)) {
    $env:CI_PROJECT_DIR = if ($env:GITHUB_WORKSPACE) { $env:GITHUB_WORKSPACE } else { (Get-Location).Path }
}
if ($env:WOA_CUDA_PATH)   { $env:PYTORCH_WIN_BUILD_CUDA_PATH   = $env:WOA_CUDA_PATH }
# The extension flow creates its own isolated venv via `python -m venv`; activate
# the per-cell venv first so a `python` is on PATH to bootstrap from.
if (-not [string]::IsNullOrWhiteSpace($VenvActivate) -and (Test-Path -LiteralPath $VenvActivate)) {
    . $VenvActivate
}

. (Join-Path $PSScriptRoot 'build-flow.ps1')
$rc = Resolve-BuildFlowExitCode (Invoke-TorchaudioWindowsBuildFlow)
if ($rc -ne 0) {
    Write-Host "::error title=woa torchaudio build::torchaudio build-flow returned $rc"
    exit $rc
}

# Collect the torchaudio wheel (built into <WHEEL_OUT_ROOT>\cuda_embed_dlls) into OutputDir.
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$marker = Join-Path (Join-Path $env:CI_PROJECT_DIR 'logs') 'WHEEL_OUT_ROOT'
if (-not (Test-Path -LiteralPath $marker)) {
    Write-Host "::error title=woa torchaudio build::WHEEL_OUT_ROOT marker not found at $marker"
    exit 1
}
$embedDir = Join-Path ((Get-Content -LiteralPath $marker -Raw).Trim()) 'cuda_embed_dlls'
$whl = Get-ChildItem -LiteralPath $embedDir -Filter 'torchaudio-*.whl' -File -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $whl) {
    Write-Host "::error title=woa torchaudio build::no torchaudio wheel under $embedDir"
    exit 1
}
Copy-Item -LiteralPath $whl.FullName -Destination $OutputDir -Force
Write-Host "Staged torchaudio wheel -> $OutputDir\$($whl.Name)"
exit 0
