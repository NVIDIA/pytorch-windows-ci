#Requires -Version 5.1
<#
.SYNOPSIS
  Capture / restore the full process env block. Carved out of CiCommon.ps1.

.DESCRIPTION
  Used by Pester tests to isolate env mutation across cases (Push in BeforeEach,
  Pop in AfterEach) and by CI retry paths that need a known-good env baseline.

  No dependencies; safe to dot-source standalone.
#>

function Push-CiEnvSnapshot {
    <#
    .SYNOPSIS
      Capture the current process env block. Returns a snapshot handle for Pop-CiEnvSnapshot.

    .DESCRIPTION
      Snapshots the full process-scope env map at call time. Use this for:
        * Pester unit tests of code that mutates env (Push-CiEnvSnapshot in BeforeEach,
          Pop-CiEnvSnapshot in AfterEach).
        * CI job retries that need to restore a known-good env baseline between attempts.

      The handle is a hashtable with a 'Snapshot' (key -> value map) and 'TakenAt' (UTC).
      The captured values are copied — mutating env after Push does not affect the snapshot.
    #>
    [CmdletBinding()]
    [OutputType([hashtable])]
    param()
    $snapshot = @{}
    foreach ($entry in [Environment]::GetEnvironmentVariables('Process').GetEnumerator()) {
        $snapshot[[string]$entry.Key] = [string]$entry.Value
    }
    return @{ Snapshot = $snapshot; TakenAt = (Get-Date).ToUniversalTime() }
}

function Pop-CiEnvSnapshot {
    <#
    .SYNOPSIS
      Restore the process env block to a previously captured snapshot.

    .DESCRIPTION
      Variables present at snapshot time are written back to their captured values; variables
      that were created after the snapshot are unset. The function does not throw on a per-key
      restoration write failure (defensive — some platform vars may be read-only), but it
      DOES surface a Write-Warning summary so a partial restore is never silent. Per-key
      detail is also emitted via Write-Verbose for diagnostics.
    #>
    [CmdletBinding()]
    [OutputType([int])]
    param([Parameter(Mandatory)][hashtable] $Snapshot)
    if (-not $Snapshot.ContainsKey('Snapshot')) {
        throw "Pop-CiEnvSnapshot: invalid snapshot handle (missing 'Snapshot' key)."
    }
    $expected = [hashtable]$Snapshot.Snapshot

    $failures = [System.Collections.Generic.List[string]]::new()

    $currentKeys = @([Environment]::GetEnvironmentVariables('Process').Keys | ForEach-Object { [string]$_ })
    foreach ($key in $currentKeys) {
        if (-not $expected.ContainsKey($key)) {
            try { [Environment]::SetEnvironmentVariable($key, $null, 'Process') }
            catch {
                $failures.Add("unset '$key': $($_.Exception.Message)") | Out-Null
                Write-Verbose "Pop-CiEnvSnapshot: failed to unset '$key': $_"
            }
        }
    }
    foreach ($entry in $expected.GetEnumerator()) {
        try { [Environment]::SetEnvironmentVariable([string]$entry.Key, [string]$entry.Value, 'Process') }
        catch {
            $failures.Add("restore '$($entry.Key)': $($_.Exception.Message)") | Out-Null
            Write-Verbose "Pop-CiEnvSnapshot: failed to restore '$($entry.Key)': $_"
        }
    }

    if ($failures.Count -gt 0) {
        $sample = ($failures | Select-Object -First 5) -join '; '
        Write-Warning ("Pop-CiEnvSnapshot: {0} per-key write failure(s) (first up to 5: {1}). " +
            "Use -Verbose for the full list." -f $failures.Count, $sample)
    }

    return $failures.Count
}
