# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Download exact released wheels from pypi.nvidia.com and smoke-test them.
.DESCRIPTION
  This helper does not call Kitmaker. It can be run locally against an already
  published torch/torchaudio/torchvision wheel set.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string[]] $WheelFileName,
    [Parameter(Mandatory)][string] $PythonExecutable,
    [string] $IndexUrl = 'https://pypi.nvidia.com/nvtorch_oot_nightly',
    [string] $ExtraIndexUrl = 'https://pypi.org/simple',
    [string] $ExpectedHashesJson = '',
    [string] $ReportPath = '',
    [int] $PollSeconds = 15,
    [int] $TimeoutSeconds = 900
)

$ErrorActionPreference = 'Stop'

function Get-WheelRequirement {
    param([Parameter(Mandatory)][string] $FileName)
    if ($FileName -notmatch '^(torch|torchaudio|torchvision)-([^-]+)-.+-win_arm64\.whl$') {
        throw "Unsupported release wheel filename: $FileName"
    }
    return [pscustomobject]@{ package = $Matches[1]; version = $Matches[2]; file = $FileName }
}

if ($WheelFileName.Count -ne 3 -or
    (@($WheelFileName | ForEach-Object { (Get-WheelRequirement $_).package } |
            Sort-Object -Unique) -join ',') -ne 'torch,torchaudio,torchvision') {
    throw 'PyPI verification requires exactly one torch, torchaudio, and torchvision wheel.'
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Python executable not found: $PythonExecutable"
}
if ($PollSeconds -le 0 -or $TimeoutSeconds -le 0) {
    throw 'PollSeconds and TimeoutSeconds must be positive.'
}

$requirements = @($WheelFileName | ForEach-Object { Get-WheelRequirement $_ })
$expectedHashes = @{}
if (-not [string]::IsNullOrWhiteSpace($ExpectedHashesJson)) {
    $hashData = $ExpectedHashesJson | ConvertFrom-Json
    foreach ($property in $hashData.psobject.Properties) {
        $expectedHashes[$property.Name] = [string]$property.Value
    }
}
$workRoot = Join-Path ([IO.Path]::GetTempPath()) ("pytorch-pypi-verify-{0}" -f [guid]::NewGuid().ToString('N'))
$downloadRoot = Join-Path $workRoot 'download'
$venvRoot = Join-Path $workRoot 'venv'
try {
    New-Item -ItemType Directory -Path $downloadRoot -Force | Out-Null
    foreach ($requirement in $requirements) {
        $timer = [Diagnostics.Stopwatch]::StartNew()
        while ($true) {
            & $PythonExecutable -m pip download --disable-pip-version-check --no-input `
                --no-cache-dir --only-binary=:all: --no-deps --index-url $IndexUrl `
                --dest $downloadRoot "$($requirement.package)==$($requirement.version)" 2>&1 |
                Out-Host
            if ($LASTEXITCODE -eq 0) { break }
            if ($timer.Elapsed.TotalSeconds -ge $TimeoutSeconds) {
                throw "Could not download $($requirement.package)==$($requirement.version) from $IndexUrl within $TimeoutSeconds seconds."
            }
            Write-Information ("[pypi-verify][INFO] waiting for {0}=={1}; elapsed_seconds={2}" -f `
                    $requirement.package, $requirement.version, [int]$timer.Elapsed.TotalSeconds) `
                -InformationAction Continue
            Start-Sleep -Seconds $PollSeconds
        }
    }

    $downloaded = @(Get-ChildItem -LiteralPath $downloadRoot -File -Filter '*.whl')
    if ($downloaded.Count -ne 3 -or
        (@($downloaded.Name | Sort-Object) -join ',') -ne (@($WheelFileName | Sort-Object) -join ',')) {
        throw "PyPI returned an unexpected wheel set: $($downloaded.Name -join ', ')."
    }
    foreach ($wheel in $downloaded) {
        if ($expectedHashes.ContainsKey($wheel.Name)) {
            $actual = (Get-FileHash -LiteralPath $wheel.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            if (-not [string]::Equals($actual, $expectedHashes[$wheel.Name],
                    [StringComparison]::OrdinalIgnoreCase)) {
                throw "PyPI checksum mismatch for $($wheel.Name): expected=$($expectedHashes[$wheel.Name]) actual=$actual"
            }
        }
    }

    & $PythonExecutable -m venv $venvRoot 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the PyPI verification environment.' }
    $venvPython = Join-Path $venvRoot 'Scripts\python.exe'
    $installArguments = @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input',
        '--index-url', $IndexUrl)
    if (-not [string]::IsNullOrWhiteSpace($ExtraIndexUrl)) {
        $installArguments += @('--extra-index-url', $ExtraIndexUrl)
    }
    $installArguments += @($downloaded.FullName)
    & $venvPython @installArguments 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Installing the released wheel set failed.' }

    $smoke = @'
import torch, torchaudio, torchvision
assert torch.cuda.is_available(), "CUDA is unavailable"
x = torch.ones(2, device="cuda")
assert float((x + 1).sum().cpu()) == 4.0
print("pypi-release-smoke-ok", torch.__version__, torchaudio.__version__, torchvision.__version__, torch.cuda.get_device_name(0))
'@
    & $venvPython -c $smoke 2>&1 | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Released PyTorch/CUDA smoke test failed.' }

    $report = [pscustomobject][ordered]@{
        index_url = $IndexUrl
        verified_at = [DateTimeOffset]::UtcNow.ToString('o')
        wheels = @($downloaded | ForEach-Object {
            [pscustomobject]@{
                filename = $_.Name
                size = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        })
        smoke = 'passed'
    }
    if (-not [string]::IsNullOrWhiteSpace($ReportPath)) {
        $parent = Split-Path -Parent $ReportPath
        if (-not [string]::IsNullOrWhiteSpace($parent)) {
            New-Item -ItemType Directory -Path $parent -Force | Out-Null
        }
        $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ReportPath -Encoding utf8
    }
    return $report
}
finally {
    Remove-Item -LiteralPath $workRoot -Recurse -Force -ErrorAction SilentlyContinue
}
