# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

<#
.SYNOPSIS
  Small helpers shared by torchaudio and torchvision Windows wheel scripts.

.DESCRIPTION
  - Invoke-ExtensionPipWheelLogged: pip wheel with stdout/stderr captured to a log file (delegates
    to Invoke-CmdLogged for cmd.exe quoting).
  - Clear-ExtensionRepoBuildDir: remove the repo's build/ tree so a second pip wheel links cleanly.
  - Assert-ExtensionWhlOutput: fail if no matching wheels; print a table for job logs.
  - Get-ExtensionWheelBuildWorkRoot / Invoke-ExtensionGitShallowClone: shorter paths + long-path
    git clone.
  - Get-ExtensionRepoBuildPackageDir: newest build\lib.win-arm64-cpython-*\<package> after pip wheel.
  - Get-VcpkgInstalledBinDirFromRoot: resolve .../bin or .../arm64-windows\bin under a vcpkg
    installed root.

.NOTES
  Set EXTENSION_WIN_WORK_PARENT to a short path (e.g. C:\xb) if a clone fails with `Filename too long`.
#>

. (Join-Path $PSScriptRoot '..' 'env' 'EnvResolve.ps1')
. (Join-Path $PSScriptRoot '..' 'log' 'Phase.ps1')
. (Join-Path $PSScriptRoot '..' 'workflow' 'LoggedExec.ps1')

function Get-ExtensionWheelBuildWorkRoot {
    <#
    .SYNOPSIS
      Directory for venv and shallow clone (_ext_<name>_<jobid>).

    .PARAMETER ExtName
      torchaudio or torchvision (folder prefix).
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][string] $ExtName)

    $jid = Resolve-CiEnv -Name 'CI_JOB_ID'
    if ([string]::IsNullOrWhiteSpace($jid)) {
        $jid = 'local'
    }
    $parent = Resolve-CiEnv -Name 'EXTENSION_WIN_WORK_PARENT' -Default (Get-CiDefault ExtensionWinWorkParent)
    if ([string]::IsNullOrWhiteSpace($parent)) {
        $parent = Resolve-CiEnv -Name 'CI_PROJECT_DIR'
    }
    if ([string]::IsNullOrWhiteSpace($parent)) {
        $parent = (Get-Location).ProviderPath
    }
    $parent = $parent.TrimEnd('\', '/')
    if (-not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    return (Join-Path $parent ("_ext_{0}_{1}" -f $ExtName, $jid))
}

function Invoke-ExtensionGitShallowClone {
    <#
    .SYNOPSIS
      Shallow checkout with core.longpaths=true for Windows MAX_PATH during checkout.

    .DESCRIPTION
      With -Ref (a full commit SHA) the exact commit is fetched with --depth 1 and checked
      out detached, so the build is reproducible and integrity-checked (Git objects are
      content-addressed). Without -Ref it shallow-clones the remote's default branch.
      Sets $LASTEXITCODE non-zero on the first failing git step so the caller can throw.

    .PARAMETER RemoteUrl
      Git remote URL.

    .PARAMETER LocalDirectoryName
      Target directory under cwd (e.g. audio or vision).

    .PARAMETER Ref
      Full commit SHA to pin; empty = the remote default branch.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $RemoteUrl,
        [Parameter(Mandatory)][string] $LocalDirectoryName,
        [string] $Ref
    )
    if ([string]::IsNullOrWhiteSpace($Ref)) {
        & git @(
            'clone',
            '--config', 'core.longpaths=true',
            '--depth', '1',
            $RemoteUrl,
            $LocalDirectoryName
        )
        return
    }
    # Pinned commit: init + fetch that exact SHA (shallow) + detached checkout.
    New-Item -ItemType Directory -Path $LocalDirectoryName -Force | Out-Null
    & git @('-C', $LocalDirectoryName, '-c', 'init.defaultBranch=main', 'init', '-q')
    if ($LASTEXITCODE -ne 0) { return }
    & git @('-C', $LocalDirectoryName, 'config', 'core.longpaths', 'true')
    if ($LASTEXITCODE -ne 0) { return }
    & git @('-C', $LocalDirectoryName, 'remote', 'add', 'origin', $RemoteUrl)
    if ($LASTEXITCODE -ne 0) { return }
    & git @('-C', $LocalDirectoryName, 'fetch', '--depth', '1', 'origin', $Ref)
    if ($LASTEXITCODE -ne 0) { return }
    & git @('-C', $LocalDirectoryName, 'checkout', '--detach', 'FETCH_HEAD')
}

function Invoke-ExtensionPipInstallRetried {
    <#
    .SYNOPSIS
      Run `python -m pip <args>` in the active venv, retrying a failed attempt.

    .DESCRIPTION
      pip runs here as an ordinary process, so it can die for reasons that have
      nothing to do with the packages named. One arm64 build lost torchvision
      when `pip install -r woa-base.txt` exited -1073741819 - 0xC0000005, an
      access violation - printing no pip diagnostics at all, because the
      process crashed rather than reporting a problem. Unprotected, a single
      such crash reds the build job and, with it, the cell.

      Retries every nonzero exit rather than trying to sort a crash from a real
      error. pip already retries network failures inside one attempt, so what
      normally survives to here is a genuine resolution failure - and that
      fails identically on each attempt and still ends up thrown, costing only
      seconds apiece against a pinned requirements file.

      Throws on exhaustion, so callers keep the failure semantics they had when
      they checked $LASTEXITCODE themselves.

    .PARAMETER PipArgs
      Arguments after `-m pip`, e.g. @('install', '-r', $reqs).

    .PARAMETER What
      Human-readable subject for the warning and throw text.

    .PARAMETER Attempts
      Total attempts, including the first.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string[]] $PipArgs,
        [Parameter(Mandatory)][string] $What,
        [int] $Attempts = 3,
        [int[]] $BackoffSeconds = @(15, 30)
    )
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        python -m pip @PipArgs
        $rc = $LASTEXITCODE
        if ($rc -eq 0) { return }

        # A negative code is an NTSTATUS surfaced as int32, so it says the
        # process died where a positive one says pip reported a problem. Both
        # are retried; they are named apart because they want different
        # follow-up when the retries do not save it.
        $kind = if ($rc -lt 0) { 'crashed with 0x{0:X8}' -f $rc } else { "failed with exit $rc" }
        if ($attempt -ge $Attempts) {
            throw "$What $kind after $Attempts attempt(s)."
        }
        $delay = $BackoffSeconds[[Math]::Min($attempt - 1, $BackoffSeconds.Count - 1)]
        Write-Warning "$What $kind on attempt $attempt/$Attempts; retrying in ${delay}s."
        Start-Sleep -Seconds $delay
    }
}

function Invoke-ExtensionPipWheelLogged {
    <#
    .SYNOPSIS
      Run pip wheel from RepoRoot into WheelOutDir; capture stdout/stderr to LogPath.

    .PARAMETER RepoRoot
      Extension source root (e.g. .../audio or .../vision).

    .PARAMETER WheelOutDir
      Destination directory for .whl files (created if missing).

    .PARAMETER LogPath
      Path to capture pip wheel stdout/stderr (the logs/ artifact).
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $RepoRoot,
        [Parameter(Mandatory)][string] $WheelOutDir,
        [Parameter(Mandatory)][string] $LogPath
    )
    New-Item -ItemType Directory -Path $WheelOutDir -Force | Out-Null
    Write-CiPhase -State 'INFO' -Phase 'pip_wheel_log' -Component 'extension-build' `
        -Detail "pip wheel log: $LogPath"
    $wEsc = $WheelOutDir.Replace('"', '""')
    Invoke-CmdLogged `
        -Command ("python -m pip wheel . --no-deps --no-build-isolation -v -w `"$wEsc`"") `
        -LogPath $LogPath `
        -WorkingDirectory $RepoRoot `
        -FailureMessage 'pip wheel failed'
}

function Clear-ExtensionRepoBuildDir {
    <#
    .SYNOPSIS
      Delete RepoRoot/build if present (before reinstalling a different torch wheel).

    .PARAMETER RepoRoot
      Cloned extension repository root.
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][string] $RepoRoot)
    $buildDir = Join-Path $RepoRoot 'build'
    if (Test-Path -LiteralPath $buildDir) {
        Remove-Item -LiteralPath $buildDir -Recurse -Force -ErrorAction Stop
    }
}

function Assert-ExtensionWhlOutput {
    <#
    .SYNOPSIS
      Require at least one wheel under WheelDir; print a table and return the count.

    .PARAMETER WheelDir
      Directory to scan (e.g. WHEEL_OUT_ROOT or .../cuda_embed_dlls); use -Filter for package wheels.

    .PARAMETER Filter
      Wildcard for Get-ChildItem -Filter (default all .whl).
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param(
        [Parameter(Mandatory)][string] $WheelDir,
        [string] $Filter = '*.whl'
    )
    $items = @(Get-ChildItem -LiteralPath $WheelDir -Filter $Filter -File -ErrorAction SilentlyContinue)
    if ($items.Count -lt 1) {
        throw "No wheels matching '$Filter' under $WheelDir"
    }
    $items | Format-Table Name, Length, LastWriteTime -AutoSize | Out-Host
    return $items.Count
}

function Get-ExtensionRepoBuildPackageDir {
    <#
    .SYNOPSIS
      Locate build\lib.win-arm64-cpython-*\<PackageDirName> (newest plat dir wins).

    .PARAMETER RepoRoot
      Cloned repo root (e.g. .../vision).

    .PARAMETER PackageDirName
      Package folder under the plat dir (e.g. vision for torchvision).
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $RepoRoot,
        [Parameter(Mandatory)][string] $PackageDirName
    )
    $buildDir = Join-Path $RepoRoot 'build'
    if (-not (Test-Path -LiteralPath $buildDir)) {
        return $null
    }
    $platDirs = @(
        Get-ChildItem -LiteralPath $buildDir -Directory -Filter 'lib.win-arm64-cpython-*' -ErrorAction SilentlyContinue |
            Sort-Object -Property LastWriteTime -Descending
    )
    foreach ($p in $platDirs) {
        $pkg = Join-Path $p.FullName $PackageDirName
        if (Test-Path -LiteralPath $pkg) {
            return $pkg
        }
    }
    return $null
}

function Get-VcpkgInstalledBinDirFromRoot {
    <#
    .SYNOPSIS
      First existing path among InstalledRoot\bin and InstalledRoot\arm64-windows\bin.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([string] $InstalledRoot)

    if ([string]::IsNullOrWhiteSpace($InstalledRoot)) {
        return $null
    }
    $root = $InstalledRoot.TrimEnd('\', '/')
    $candidates = @(
        (Join-Path $root 'bin'),
        (Join-Path (Join-Path $root 'arm64-windows') 'bin')
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) {
            return $c
        }
    }
    return $null
}
