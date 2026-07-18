# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Invoke-CmdLogged — run a shell command via cmd.exe; tee the output; throw on non-zero exit.

.DESCRIPTION
  Carved out of CiWorkflow.ps1. No CI helper deps; self-contained.
#>

function Invoke-CmdLogged {
    <#
    .SYNOPSIS
      Run a shell command via cmd.exe; tee stdout+stderr to a log file; throw on non-zero exit.

    .DESCRIPTION
      Wraps the call-operator + `cmd.exe /d /s /c "<command>"` pattern that every pip-wheel /
      delvewheel site in the codebase uses. The cmd.exe layer is intentional: it preserves the
      double-quote-doubling escape (``"`") that build commands rely on when they embed paths
      with spaces.

      Output is tee'd to -LogPath (parent dir auto-created) and also echoed live to the host
      stream so an operator watching the job log sees progress. The full transcript is preserved
      for the `logs/` artifact.

      Exit code: cmd.exe reports the underlying command's exit code via $LASTEXITCODE. When
      non-zero, the function throws "<FailureMessage> (exit <N>; see <LogPath>)". The log path
      is included in the error so triage tooling can fetch it without parsing the rest of the
      job log.

    .PARAMETER Command
      Full command line as a single string (e.g. 'python -m pip wheel . --no-deps -v -w "<dir>"').
      Quote any path that may contain spaces.

    .PARAMETER LogPath
      Destination file for the merged stdout/stderr capture. Parent directory is created if
      missing. Existing content is overwritten.

    .PARAMETER WorkingDirectory
      Directory cmd.exe runs in. Defaults to the caller's current location (Push-Location is
      used so the caller's pwd is restored on every code path).

    .PARAMETER FailureMessage
      Prefix for the thrown exception when the command exits non-zero. Defaults to a generic
      "command failed".
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $Command,
        [Parameter(Mandatory)][string] $LogPath,
        [string] $WorkingDirectory,
        [string] $FailureMessage = 'command failed'
    )

    $parent = Split-Path -Parent $LogPath
    if (-not [string]::IsNullOrWhiteSpace($parent) -and -not (Test-Path -LiteralPath $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    $cwd = if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
        (Get-Location).ProviderPath
    } else {
        $WorkingDirectory
    }

    Push-Location -LiteralPath $cwd
    try {
        # cmd /d /s /c keeps the command line verbatim after the first /c. 2>&1 merges stderr
        # into stdout inside cmd so a single Tee-Object captures both streams in order.
        #
        # Out-Host is load-bearing, not decorative: the WoA flows run inside a captured
        # subexpression - `Resolve-BuildFlowExitCode (Invoke-*Flow)` - which swallows the whole
        # success stream to extract the trailing exit code (see shared/workflow/FlowExitCode.ps1).
        # A bare `| Tee-Object` leaves its passthrough on the success stream, so the pip-wheel /
        # delvewheel transcript is captured into the flow's return value and NEVER reaches the job
        # log. Piping to Out-Host renders each line to the console (like Write-Host, bypassing the
        # captured success stream) so an operator sees live progress, and it keeps that transcript
        # out of the flow's return value. $LASTEXITCODE still reflects cmd.exe (cmdlets don't touch it).
        & cmd.exe /d /s /c "$Command 2>&1" 2>&1 | Tee-Object -FilePath $LogPath | Out-Host
        $exit = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }

    if ($null -ne $exit -and $exit -ne 0) {
        throw "$FailureMessage (exit $exit; see $LogPath)"
    }
}
