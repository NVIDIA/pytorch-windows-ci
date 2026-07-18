#Requires -Version 5.1
<#
.SYNOPSIS
  Synthesize a failing JUnit report whenever a shard produced no real JUnit for a reason triage
  cannot otherwise see - a watchdog-killed hang, a broken-wheel import failure, or a crash that
  wrote zero reports - so the failure is visible in triage instead of vanishing.

.DESCRIPTION
  The triage report is built purely from the *.xml a shard uploads. Several failure modes leave the
  shard with no (or partial) JUnit, so without a placeholder the shard is silently absent from
  triage even though it failed (a false green at the report level):

    * Watchdog kill: Invoke-RunTestPython's watchdog does `taskkill /T /F` on the whole run_test.py
      -> pytest tree. pytest only writes its JUnit at session end (pytest_sessionfinish), so a hard
      kill mid-session flushes NOTHING.
    * torch import failure: run_test.py imports torch at module scope, so a DLL-incomplete wheel
      (e.g. WinError 126) dies before any test runs -> zero reports.
    * zero reports: a crash-on-load or native abort ends the run before any XML flushes.

  For the watchdog case we can attribute the hang to a concrete test: PyTorch's StepcurrentPlugin
  (test/conftest.py) records the running test's nodeid to
  <repoRoot>\.pytest_cache\v\cache\stepcurrent\<key>\lastrun on every pytest_runtest_protocol (the
  same file run_test.py reads to resume after a crash). We harvest that nodeid and emit a minimal
  failing JUnit so publish-test-reports.sh finds an *.xml to upload and triage names the culprit.

  Attribution caveat (watchdog): under pytest-xdist (`-n > 1`) every worker overwrites `lastrun`, so
  the nodeid is the test running *closest to* the hang, exact only when a single worker is active.
  Exact attribution otherwise comes from the per-test pytest-timeout; this harvester is the backstop
  for the pure watchdog-kill path where no JUnit was written at all.

  For the import-failure / zero-report cases there is no per-test attribution, so
  Save-ShardFailurePlaceholderReport writes a single failing testcase naming the shard and the
  reason, which keeps the shard visible (and red) in triage.

  Pure helpers: no env reads, no Write-CiPhase - the caller (pytorch-windows-test-shard.ps1) logs.
#>

function Get-PytestStepcurrentNodeId {
    <#
    .SYNOPSIS
      Return the most-recently-written pytest stepcurrent `lastrun` nodeid under $RepoRoot, or $null.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $RepoRoot
    )

    $stepcurrentRoot = Join-Path $RepoRoot '.pytest_cache\v\cache\stepcurrent'
    if (-not (Test-Path -LiteralPath $stepcurrentRoot)) {
        return $null
    }

    $lastruns = @(
        Get-ChildItem -LiteralPath $stepcurrentRoot -Recurse -Filter 'lastrun' -File -ErrorAction SilentlyContinue |
            Sort-Object -Property LastWriteTimeUtc -Descending
    )

    foreach ($file in $lastruns) {
        $raw = $null
        try {
            $raw = (Get-Content -LiteralPath $file.FullName -Raw -ErrorAction Stop).Trim()
        }
        catch {
            continue
        }
        if ([string]::IsNullOrWhiteSpace($raw) -or $raw -eq 'null') {
            continue
        }

        # cache.set() writes json.dumps(nodeid) -> a quoted JSON string. Prefer a real parse, but
        # fall back to trimming the surrounding quotes if ConvertFrom-Json is unhappy.
        $nodeId = $null
        try {
            $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
            if ($parsed -is [string]) { $nodeId = $parsed }
        }
        catch {
            $nodeId = $raw.Trim('"')
        }

        if (-not [string]::IsNullOrWhiteSpace($nodeId)) {
            return $nodeId
        }
    }

    return $null
}

function ConvertTo-WatchdogJUnitClassAndName {
    <#
    .SYNOPSIS
      Split a pytest nodeid (file.py::Class::test_name[params]) into JUnit classname / name.
    #>
    [CmdletBinding()]
    [OutputType([hashtable])]
    param(
        [Parameter(Mandatory)][string] $NodeId
    )

    $parts = $NodeId -split '::'
    if ($parts.Count -ge 2) {
        $name = $parts[-1]
        $classSegments = $parts[0..($parts.Count - 2)]
        $classSegments[0] = ($classSegments[0] -replace '\.py$', '')
        $className = ($classSegments -join '.')
    }
    else {
        $className = ($NodeId -replace '\.py$', '')
        $name = 'unknown_test'
    }

    return @{ ClassName = $className; Name = $name }
}

function Write-SyntheticFailureJUnit {
    <#
    .SYNOPSIS
      Write a one-testcase failing JUnit under $RepoRoot\test\test-reports\<SubDir>. Returns the XML
      path. This is the shared writer behind every placeholder report (watchdog, import failure,
      zero reports); the JUnit shape matches what publish-test-reports.sh discovers and triage parses.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $RepoRoot,
        [Parameter(Mandatory)][string] $SubDir,
        [Parameter(Mandatory)][string] $SuiteName,
        [Parameter(Mandatory)][string] $ClassName,
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][string] $Message,
        [Parameter(Mandatory)][string] $Detail,
        [Parameter()][string] $FilePrefix = 'synthetic_failure'
    )

    $reportsDir = Join-Path $RepoRoot (Join-Path 'test\test-reports' $SubDir)
    New-Item -ItemType Directory -Path $reportsDir -Force | Out-Null
    $xmlPath = Join-Path $reportsDir ("{0}_{1}.xml" -f $FilePrefix, ([guid]::NewGuid().ToString('n')))

    $suiteAttr = [System.Security.SecurityElement]::Escape($SuiteName)
    $classAttr = [System.Security.SecurityElement]::Escape($ClassName)
    $nameAttr = [System.Security.SecurityElement]::Escape($Name)
    $msgAttr = [System.Security.SecurityElement]::Escape($Message)
    $bodyText = [System.Security.SecurityElement]::Escape($Detail)

    $xml = @"
<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="$suiteAttr" tests="1" failures="1" errors="0" skipped="0" time="0">
    <testcase classname="$classAttr" name="$nameAttr" time="0">
      <failure message="$msgAttr">$bodyText</failure>
    </testcase>
  </testsuite>
</testsuites>
"@

    [System.IO.File]::WriteAllText($xmlPath, $xml, (New-Object System.Text.UTF8Encoding($false)))
    return $xmlPath
}

function Write-WatchdogTimeoutJUnit {
    <#
    .SYNOPSIS
      Write a one-testcase failing JUnit into $RepoRoot\test\test-reports\watchdog-timeout. Returns
      the XML path.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $RepoRoot,
        [Parameter(Mandatory)][int] $TimeoutSec,
        [Parameter()][AllowNull()][AllowEmptyString()][string] $NodeId,
        [Parameter()][string] $Shard = '',
        [Parameter()][string] $NumShards = ''
    )

    if ([string]::IsNullOrWhiteSpace($NodeId)) {
        $className = 'run_test_watchdog'
        $name = 'watchdog_timeout_unattributed'
        $detail = "run_test.py wall-clock watchdog timeout after ${TimeoutSec}s; the process tree was killed before any test could be attributed (pytest stepcurrent cache was empty or unreadable). See the shard log for the last RUNNING line."
    }
    else {
        $split = ConvertTo-WatchdogJUnitClassAndName -NodeId $NodeId
        $className = $split.ClassName
        $name = $split.Name
        $detail = "run_test.py wall-clock watchdog timeout after ${TimeoutSec}s; the process tree was killed. This is the last test recorded by pytest stepcurrent (the test running closest to the hang; exact under a single worker). Original nodeid: ${NodeId}"
    }

    $shardLabel = if ([string]::IsNullOrWhiteSpace($Shard)) { '' } else { " shard ${Shard}/${NumShards}" }
    $message = "WATCHDOG_TIMEOUT after ${TimeoutSec}s${shardLabel}"

    return Write-SyntheticFailureJUnit -RepoRoot $RepoRoot -SubDir 'watchdog-timeout' `
        -SuiteName 'run_test_watchdog_timeout' -ClassName $className -Name $name `
        -Message $message -Detail $detail -FilePrefix 'watchdog_timeout'
}

function Save-WatchdogTimeoutReport {
    <#
    .SYNOPSIS
      Harvest the stepcurrent nodeid and write the synthetic failing JUnit in one call. Returns an
      object with NodeId (may be $null) and ReportPath.
    #>
    [CmdletBinding()]
    [OutputType([psobject])]
    param(
        [Parameter(Mandatory)][string] $RepoRoot,
        [Parameter(Mandatory)][int] $TimeoutSec,
        [Parameter()][string] $Shard = '',
        [Parameter()][string] $NumShards = ''
    )

    $nodeId = Get-PytestStepcurrentNodeId -RepoRoot $RepoRoot
    $path = Write-WatchdogTimeoutJUnit -RepoRoot $RepoRoot -TimeoutSec $TimeoutSec -NodeId $nodeId `
        -Shard $Shard -NumShards $NumShards

    return [PSCustomObject]@{
        NodeId     = $nodeId
        ReportPath = $path
    }
}

function Save-ShardFailurePlaceholderReport {
    <#
    .SYNOPSIS
      Write a synthetic failing JUnit for a shard that produced no real report for a non-timeout
      reason (torch import failure, or a crash that flushed zero JUnit), so the shard stays visible
      (and red) in triage instead of silently vanishing. Returns an object with ReportPath.

    .DESCRIPTION
      Unlike the watchdog path there is no per-test attribution here - the failure is the shard's
      whole run. $Component becomes the JUnit classname (so triage groups it as a module, e.g.
      'torch_import_check' / 'run_test_no_reports'), $TestName the testcase name, $Summary the
      one-line <failure message>, and $Detail the failure body. Written under
      test\test-reports\shard-failure so publish-test-reports.sh uploads it like any other report.
    #>
    [CmdletBinding()]
    [OutputType([psobject])]
    param(
        [Parameter(Mandatory)][string] $RepoRoot,
        [Parameter(Mandatory)][string] $Component,
        [Parameter(Mandatory)][string] $TestName,
        [Parameter(Mandatory)][string] $Summary,
        [Parameter(Mandatory)][string] $Detail
    )

    $path = Write-SyntheticFailureJUnit -RepoRoot $RepoRoot -SubDir 'shard-failure' `
        -SuiteName 'shard_failure_placeholder' -ClassName $Component -Name $TestName `
        -Message $Summary -Detail $Detail -FilePrefix 'shard_failure'

    return [PSCustomObject]@{ ReportPath = $path }
}
