# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

<#
.SYNOPSIS
    Create a FRESH per-job Python virtual environment for the Windows-on-Arm PyTorch CI.

.DESCRIPTION
    The WoA runners keep only clean ARM64 CPython interpreters (provisioned via winget);
    they no longer carry pre-built venvs.
    Because the shared runners are public-facing and may see untrusted code, every job
    builds its own throwaway venv under the job scratch tree (wiped by woa-strict-clean at
    job start and end), so nothing a job installs can leak into the next job.

    This script, invoked by the `woa-create-venv` composite action:
      1. resolves the ARM64-native interpreter for the requested version (regular python.exe
         or free-threaded python<major>.<minor>t.exe), failing fast if only an x64 build is
         found (an x64 python3XX.lib links into the ARM64 torch_python.dll and dies with
         "unresolved external symbol __imp_PyMem_Calloc");
      2. creates the venv at -VenvPath (`python -m venv`);
      3. upgrades pip, then installs the deps in two tiers (each tier may span several files, so
         a shared base + a role-specific set compose without duplication):
           * STRICT core   (-CoreRequirements)     : one `pip install -r` per file; any failure throws.
           * BEST-EFFORT extended (-ExtendedRequirements): batch per file, then per-package retry;
             a package with no win_arm64 wheel / buildable sdist for this interpreter is skipped
             with a warning instead of aborting the venv.

    The BUILD role passes core = woa-base.txt + woa-build.txt (extended = none); the TEST role passes
    core = woa-base.txt + woa-test.txt and extended = woa-test-extended.txt (see the woa-create-venv
    action). Prints the resolved <VenvPath>\Scripts\Activate.ps1 as the last stdout line. Written to
    run under Windows PowerShell 5.1 or pwsh 7 (two-arg Join-Path only).

.NOTES
    The requirements lists are the arm64 CI's own contract (tools/woa-build/shared/requirements),
    NOT upstream .ci/docker/requirements-ci.txt, because upstream CI does not yet support win_arm64.
#>
[CmdletBinding()]
param(
    # Dotted version; a trailing 't' (e.g. '3.14t') selects the free-threaded build.
    [Parameter(Mandatory)]
    [string] $PythonVersion,

    # Label for logging / the venv folder name (py311..py314, py314t).
    [Parameter(Mandatory)]
    [string] $PythonLabel,

    # Absolute path of the venv to create (under the job scratch tree).
    [Parameter(Mandatory)]
    [string] $VenvPath,

    # Strict requirements file(s) - installed in order; any failure aborts the venv.
    [Parameter(Mandatory)]
    [string[]] $CoreRequirements,

    # Best-effort requirements file(s) - missing wheels skipped with a warning.
    [string[]] $ExtendedRequirements = @(),

    # Extra `pip install` args (e.g. an internal --index-url). No secrets here.
    [string[]] $PipExtraArgs = @(),

    # Force free-threaded resolution regardless of the version suffix.
    [switch] $FreeThreaded
)

$ErrorActionPreference = 'Stop'

function Test-Arm64Exe {
    <# PE COFF Machine field == 0xAA64 (ARM64). Reads the header only; no interpreter launch. #>
    param([Parameter(Mandatory)][string] $ExePath)
    try {
        $fs = [System.IO.File]::OpenRead($ExePath)
        try {
            $buf = New-Object byte[] 4096
            [void]$fs.Read($buf, 0, 4096)
        }
        finally { $fs.Dispose() }
        $peOff = [System.BitConverter]::ToUInt32($buf, 0x3C)
        if ($peOff -le 0 -or $peOff -gt ($buf.Length - 6)) { return $false }
        return ([System.BitConverter]::ToUInt16($buf, [int]$peOff + 4) -eq 0xAA64)
    }
    catch { return $false }
}

function Resolve-Arm64Interpreter {
    <#
        Returns the path to the ARM64-native python for $Version, or the first non-ARM64
        candidate (so the caller can report the exact x64 path), or $null when none exists.
        Mirrors the infra Resolve-PythonInterpreterForVersion selection order.
    #>
    param(
        [Parameter(Mandatory)][string] $Version,
        [switch] $Ft
    )

    $parts = $Version.Split('.')
    $tag = '{0}{1}' -f $parts[0], $parts[1]
    $exeName = if ($Ft) { "python${Version}t.exe" } else { 'python.exe' }
    $launcherArg = if ($Ft) { "-${Version}t" } else { "-$Version" }

    $found = [System.Collections.Generic.List[string]]::new()

    # 1. Windows `py` launcher (most reliable when winget registered the interpreter; may be x64).
    $py = Get-Command 'py' -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $exe = & $py.Source $launcherArg '-c' 'import sys; print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($exe)) {
                $exe = ([string]$exe).Trim()
                if (Test-Path -LiteralPath $exe) {
                    $found.Add((Get-Item -LiteralPath $exe).FullName) | Out-Null
                }
            }
        }
        catch { }
    }

    # 2. Usual winget / python.org install roots; -arm64-suffixed roots first.
    $candidates = @(
        (Join-Path $env:LocalAppData "Programs\Python\Python$tag-arm64")
        (Join-Path $env:ProgramFiles "Python$tag-arm64")
        "C:\Python$tag-arm64"
        (Join-Path $env:LocalAppData "Programs\Python\Python$tag")
        (Join-Path $env:ProgramFiles "Python$tag")
        "C:\Python$tag"
    )
    foreach ($root in $candidates) {
        if ([string]::IsNullOrWhiteSpace($root)) { continue }
        $c = Join-Path $root $exeName
        if (Test-Path -LiteralPath $c) {
            $found.Add((Get-Item -LiteralPath $c).FullName) | Out-Null
        }
    }

    foreach ($f in $found) {
        if (Test-Arm64Exe -ExePath $f) { return $f }
    }
    if ($found.Count -gt 0) { return $found[0] }
    return $null
}

function Invoke-PipStrict {
    param([string] $VenvPython, [string[]] $PipArgs)
    & $VenvPython @PipArgs
    if ($LASTEXITCODE -ne 0) {
        throw "New-WoaJobVenv: strict 'pip $($PipArgs -join ' ')' failed (exit $LASTEXITCODE)."
    }
}

# --- normalize free-threaded selection (version suffix 't', label suffix 't', or -FreeThreaded) ---
$ver = $PythonVersion.Trim()
$ft = [bool]$FreeThreaded
if ($ver.EndsWith('t')) { $ft = $true; $ver = $ver.Substring(0, $ver.Length - 1) }
if ($PythonLabel.Trim().EndsWith('t')) { $ft = $true }
if ($ver -notmatch '^\d+\.\d+$') {
    throw "New-WoaJobVenv: PythonVersion '$PythonVersion' is not 'major.minor' (optionally 't'-suffixed), e.g. 3.13 or 3.14t."
}
$interpDesc = if ($ft) { "$ver (free-threaded)" } else { $ver }

Write-Host "== WoA create venv (label=$PythonLabel, python=$interpDesc) =="

# --- resolve + hard-require an ARM64 interpreter ---
$pythonExe = Resolve-Arm64Interpreter -Version $ver -Ft:$ft
if ([string]::IsNullOrWhiteSpace($pythonExe)) {
    $hint = if ($ft) {
        "Install Python.Python.$ver with Include_freethreaded=1 (--architecture arm64) so python${ver}t.exe exists."
    }
    else {
        "Install Python.Python.$ver --architecture arm64."
    }
    throw "New-WoaJobVenv: no Python $interpDesc interpreter found on this runner. $hint"
}
if (-not (Test-Arm64Exe -ExePath $pythonExe)) {
    throw ("New-WoaJobVenv: Python $interpDesc resolved to '$pythonExe', which is NOT ARM64-native " +
        '(PE machine != 0xAA64). WoA builds win_arm64 wheels; a venv backed by an x64 interpreter fails ' +
        "the torch_python link with 'unresolved external symbol __imp_PyMem_Calloc'. Reprovision the " +
        "runner with the ARM64 CPython (winget install Python.Python.$ver --architecture arm64) and remove " +
        'any x64 install of the same version that shadows it.')
}
Write-Host "  interpreter: $pythonExe (ARM64-native)"

# --- create the fresh venv (parent under job scratch is wiped each job) ---
$parent = Split-Path -Parent $VenvPath
if (-not [string]::IsNullOrWhiteSpace($parent) -and -not (Test-Path -LiteralPath $parent)) {
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
}
if (Test-Path -LiteralPath $VenvPath) {
    Write-Host "  removing stale venv dir: $VenvPath"
    Remove-Item -LiteralPath $VenvPath -Recurse -Force -ErrorAction SilentlyContinue
}
Write-Host "  creating venv: $VenvPath"
& $pythonExe -m venv $VenvPath
if ($LASTEXITCODE -ne 0) { throw "New-WoaJobVenv: 'python -m venv' failed (exit $LASTEXITCODE) for $VenvPath." }

$venvPy = Join-Path $VenvPath 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $venvPy)) {
    throw "New-WoaJobVenv: venv created but $venvPy is missing."
}

# --- upgrade pip ---
Write-Host '  upgrading pip...'
Invoke-PipStrict -VenvPython $venvPy -PipArgs (@('-m', 'pip', 'install', '--upgrade', 'pip') + $PipExtraArgs)

# --- strict core (any failure aborts the job); each file installed in order ---
foreach ($rf in $CoreRequirements) {
    if ([string]::IsNullOrWhiteSpace($rf)) { continue }
    if (-not (Test-Path -LiteralPath $rf)) {
        throw "New-WoaJobVenv: core requirements file not found: $rf"
    }
    Write-Host "  installing core requirements ($rf)..."
    Invoke-PipStrict -VenvPython $venvPy -PipArgs (@('-m', 'pip', 'install', '-r', $rf) + $PipExtraArgs)
}

# --- best-effort extended (batch per file, then per-package retry; skip on failure, never throw) ---
$skipped = [System.Collections.Generic.List[string]]::new()
foreach ($rf in $ExtendedRequirements) {
    if ([string]::IsNullOrWhiteSpace($rf)) { continue }
    if (-not (Test-Path -LiteralPath $rf)) {
        Write-Host "  no extended requirements file at $rf (skipping)."
        continue
    }
    Write-Host "  installing extended (best-effort) requirements ($rf)..."
    & $venvPy -m pip install -r $rf @PipExtraArgs
    if ($LASTEXITCODE -eq 0) { continue }

    Write-Warning "  Extended batch install failed ($rf); retrying each package individually."
    $pkgs = Get-Content -LiteralPath $rf |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -and -not $_.StartsWith('#') }
    foreach ($pkg in $pkgs) {
        & $venvPy -m pip install $pkg @PipExtraArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "  Skipped best-effort package '$pkg' (no compatible wheel / build failed)."
            $skipped.Add($pkg) | Out-Null
        }
    }
}
if ($skipped.Count -gt 0) {
    Write-Warning "  $($skipped.Count) best-effort package(s) skipped: $($skipped -join ', ')"
}

# The best-effort installs above intentionally tolerate a failing `pip install` (a package with no
# win_arm64 wheel / buildable sdist is skipped, not fatal). A tolerated failure leaves $LASTEXITCODE
# nonzero, and the woa-create-venv composite checks $LASTEXITCODE after this script returns - so a
# skipped optional package would be misread as a venv failure. Clear the leaked native exit state
# here; strict/core failures already threw (ErrorActionPreference=Stop) and never reach this point.
$global:LASTEXITCODE = 0

$activate = Join-Path $VenvPath 'Scripts\Activate.ps1'
if (-not (Test-Path -LiteralPath $activate)) {
    throw "New-WoaJobVenv: expected activation script missing: $activate"
}
Write-Host "== venv ready: $activate =="
# Last stdout line = the activation script path (the composite action captures this).
Write-Output $activate
