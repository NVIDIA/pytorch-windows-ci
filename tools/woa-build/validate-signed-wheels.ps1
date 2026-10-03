# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Validate one cell's signed wheel set on WoA hardware before it may be published.

.DESCRIPTION
  Signing runs on a GitHub-hosted x64 runner, which can neither import win_arm64 wheels nor
  see a GPU. This is the step that proves the signed bytes still work on the platform they
  are for, and it is the only thing that can: the publish job refuses any wheel that lacks
  the passing report written here.

    1. Re-verify every native file's Authenticode signature and timestamp
       (verify-wheel-signatures.ps1), against the artifact as downloaded here.
    2. Install the three signed wheels into a brand-new venv and run a CUDA smoke test.

  The report records the SHA-256 of each wheel it validated, so a wheel swapped after this
  step no longer matches and cannot be published.

.PARAMETER WheelDir
  Directory holding the signed torch, torchaudio and torchvision wheels.

.PARAMETER VenvActivate
  Activate.ps1 of the job venv; its interpreter seeds the clean smoke-test venv.

.PARAMETER ReportPath
  Where to write validation-<cell>.json. Written on failure as well as success.

.PARAMETER ExpectedSubject
  Forwarded to verify-wheel-signatures.ps1.

.NOTES
  Exit code 0 when both checks pass, 1 otherwise.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $WheelDir,
    [Parameter(Mandatory)][string] $VenvActivate,
    [Parameter(Mandatory)][string] $ReportPath,
    [string] $ExpectedSubject = ''
)

$ErrorActionPreference = 'Stop'

$python = Join-Path (Split-Path -Parent $VenvActivate) 'python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    Write-Host "::error title=validate signed wheels::python.exe not found next to $VenvActivate"
    exit 1
}
$wheels = @(Get-ChildItem -LiteralPath $WheelDir -Filter '*.whl' -File | Sort-Object Name)
$work = Join-Path ([System.IO.Path]::GetTempPath()) ('woa-validate-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $work | Out-Null

$signatureStatus = 'failed'
$smokeStatus = 'not-run'
$smokeDetail = $null
try {
    & (Join-Path $PSScriptRoot 'verify-wheel-signatures.ps1') -WheelDir $WheelDir `
        -ReportPath (Join-Path $work 'signature-recheck.json') -ExpectedSubject $ExpectedSubject
    if ($LASTEXITCODE -eq 0) { $signatureStatus = 'passed' }

    if ($signatureStatus -eq 'passed') {
        $smokeStatus = 'failed'
        $venv = Join-Path $work 'venv'
        & $python -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw "could not create the smoke-test venv at $venv" }
        $smokePython = Join-Path $venv 'Scripts\python.exe'
        & $smokePython -m pip install --disable-pip-version-check --no-input @($wheels | ForEach-Object FullName)
        if ($LASTEXITCODE -ne 0) { throw 'installing the signed wheel set into a clean venv failed' }
        $smoke = @'
import json, torch, torchaudio, torchvision
assert torch.cuda.is_available(), "CUDA is unavailable"
x = torch.ones((2, 2), device="cuda")
assert float((x + x).sum().cpu()) == 8.0
print(json.dumps({"torch": torch.__version__, "torchaudio": torchaudio.__version__,
                  "torchvision": torchvision.__version__, "cuda": torch.version.cuda,
                  "device": torch.cuda.get_device_name(0)}))
'@
        $output = @(& $smokePython -c $smoke)
        if ($LASTEXITCODE -ne 0) { throw 'the signed-wheel CUDA smoke test failed' }
        $smokeDetail = $output[-1] | ConvertFrom-Json
        $smokeStatus = 'passed'
    }
}
catch {
    Write-Host "::error title=validate signed wheels::$($_.Exception.Message)"
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
}

$status = if ($signatureStatus -eq 'passed' -and $smokeStatus -eq 'passed') { 'passed' } else { 'failed' }
$parent = Split-Path -Parent $ReportPath
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
[pscustomobject][ordered]@{
    schema_version = 1
    status         = $status
    validated_at   = [DateTimeOffset]::UtcNow.ToString('o')
    runner         = $env:RUNNER_NAME
    signatures     = $signatureStatus
    smoke          = $smokeStatus
    smoke_detail   = $smokeDetail
    wheels         = @($wheels | ForEach-Object {
            [pscustomobject][ordered]@{
                filename = $_.Name
                sha256   = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        })
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ReportPath -Encoding utf8

if ($status -ne 'passed') { exit 1 }
Write-Host "Validated $($wheels.Count) signed wheel(s): signatures re-verified and CUDA smoke test passed."
exit 0
