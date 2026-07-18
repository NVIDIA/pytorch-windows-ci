#Requires -Version 5.1
<#
.SYNOPSIS
  Robust removal of large Windows trees that may exceed MAX_PATH.

.DESCRIPTION
  PyTorch + torchvision/torchaudio checkouts routinely include nested submodules whose
  fully-qualified paths exceed the legacy MAX_PATH (260 chars). Remove-Item can fail on those
  paths with "PathTooLongException" even on a runner that has Windows long-path support
  enabled, because the .NET FileSystem provider used under the hood does not consistently
  honour the \\?\ prefix.

  This helper offers two functions:

    Clear-DirectoryWithRobocopy <TargetDir>
        Mirrors an empty source over <TargetDir> (/MIR), so robocopy.exe — which uses the
        Win32 file APIs and natively supports paths up to 32K — performs the recursive
        delete, then removes the now-empty target. Use this as the fallback whenever
        Remove-Item -Recurse throws on a deep tree.

    Remove-StaleTree -Path <p> -Label <l> [-Component <tag>]
        Wraps the standard "try Remove-Item first, fall back to robocopy on failure" idiom
        with Write-CiPhase START/PASS/SKIP logging. Used by every cleanup entrypoint.

  Requires Write-CiPhase from ../log/Phase.ps1 to have been dot-sourced first.

  Intentionally one concern per file: anything that removes a tree on disk lives here so the
  fallback logic is single-sourced — the runner-worktree preflight cleanup and the
  pipeline-tail isolated-checkout cleanup both consume the same code path.
#>

. (Join-Path $PSScriptRoot '..' 'log' 'Phase.ps1')

function Clear-DirectoryWithRobocopy {
    <#
    .SYNOPSIS
      Mirror an empty dir over the target so robocopy deletes everything (long paths included),
      then remove the now-empty target. Workaround for Remove-Item hitting MAX_PATH.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string] $TargetDir)
    if (-not (Test-Path -LiteralPath $TargetDir)) {
        return
    }
    $empty = Join-Path ([System.IO.Path]::GetTempPath()) ('ci-empty-' + [Guid]::NewGuid().ToString('n'))
    New-Item -ItemType Directory -Path $empty -Force | Out-Null
    try {
        if ($PSCmdlet.ShouldProcess($TargetDir, 'robocopy /MIR <empty>')) {
            & robocopy.exe $empty $TargetDir /MIR /NFL /NDL /NJH /NJS /NC /NS | Out-Null
            $code = $LASTEXITCODE
            # robocopy exit codes < 8 indicate success (with various copy/skip combinations).
            # >= 8 means at least one file/dir failed to copy/delete.
            if ($code -ge 8) {
                throw "robocopy mirror failed with exit code $code for $TargetDir"
            }
            Remove-Item -LiteralPath $TargetDir -Force -Recurse -ErrorAction Stop
        }
    }
    finally {
        if (Test-Path -LiteralPath $empty) {
            Remove-Item -LiteralPath $empty -Force -Recurse -ErrorAction SilentlyContinue
        }
    }
}

function Remove-StaleTree {
    <#
    .SYNOPSIS
      Try Remove-Item; on failure (typically long paths) fall back to robocopy mirror-empty.

    .DESCRIPTION
      Emits Write-CiPhase START / PASS / SKIP lines so the cleanup decision is auditable in the
      job log. Returns silently on success or when the tree is absent; throws only when both
      Remove-Item and the robocopy fallback fail.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [Parameter(Mandatory)][string] $Path,
        [Parameter(Mandatory)][string] $Label,
        [string] $Component = 'remove-long-path-tree'
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-CiPhase -State 'SKIP' -Phase 'remove_stale_tree' -Detail "${Label} (absent)" -Component $Component
        return
    }
    if (-not $PSCmdlet.ShouldProcess($Path, "Remove-StaleTree ($Label)")) {
        return
    }
    Write-CiPhase -State 'START' -Phase 'remove_stale_tree' -Detail $Path -Component $Component
    try {
        Remove-Item -LiteralPath $Path -Force -Recurse -ErrorAction Stop
        Write-CiPhase -State 'PASS' -Phase 'remove_stale_tree' -Detail 'Remove-Item OK' -Component $Component
    }
    catch {
        Write-Warning "Remove-Item failed ($($_.Exception.Message)); trying robocopy mirror trick."
        Clear-DirectoryWithRobocopy -TargetDir $Path
        Write-CiPhase -State 'PASS' -Phase 'remove_stale_tree' -Detail 'robocopy fallback OK' -Component $Component
    }
}
