#Requires -Version 5.1
<#
.SYNOPSIS
  One canonical calendar date for a whole pipeline — used to name the dated wheel folder, bucket
  shard report uploads, and place the triage XLSX so wheels + report always co-locate.

.DESCRIPTION
  Historically the build (WheelPipeline.ps1), the shard upload (publish-test-reports.sh) and the
  triage publisher each called their own `Get-Date` / `date`. Because a nightly starts in the
  evening and its tests can finish after midnight, those three clocks disagreed across a UTC/day
  rollover and the report landed in a `<date>` folder that had no wheels ("Dated folder not found").

  This resolves a SINGLE date shared by every job in the pipeline:
    1. Operator override PYTORCH_WIN_TEST_REPORT_DATE (yyyy-MM-dd) — highest priority.
    2. CI_PIPELINE_CREATED_AT (a CI-provided per-pipeline timestamp, if set) — the same instant for
       every job in the pipeline; formatted in
       the runner's LOCAL timezone (all site runners share one TZ, and it matches the existing
       folder-name convention). Being a fixed instant, every job derives the same date regardless of
       when it actually runs.
    3. Get-Date — local-dev / non-CI fallback.

  Dependency (dot-sourced): EnvResolve.ps1 (Resolve-CiEnv).
#>

. (Join-Path $PSScriptRoot 'EnvResolve.ps1')

function Get-CiPipelineDate {
    <#
    .SYNOPSIS
      The pipeline's canonical date as a [datetime] in runner-local time (see file header for the
      resolution order).
    #>
    [CmdletBinding()]
    [OutputType([datetime])]
    param()

    $override = Resolve-CiEnv -Name 'PYTORCH_WIN_TEST_REPORT_DATE'
    if (-not [string]::IsNullOrWhiteSpace($override)) {
        $parsed = [datetime]::MinValue
        if ([datetime]::TryParseExact($override.Trim(), 'yyyy-MM-dd',
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::None, [ref]$parsed)) {
            return $parsed
        }
        Write-Warning "PYTORCH_WIN_TEST_REPORT_DATE='$override' is not yyyy-MM-dd; ignoring it."
    }

    $created = Resolve-CiEnv -Name 'CI_PIPELINE_CREATED_AT'
    if (-not [string]::IsNullOrWhiteSpace($created)) {
        $dto = [System.DateTimeOffset]::MinValue
        if ([System.DateTimeOffset]::TryParse($created.Trim(),
                [System.Globalization.CultureInfo]::InvariantCulture,
                [System.Globalization.DateTimeStyles]::AssumeUniversal, [ref]$dto)) {
            return $dto.ToLocalTime().DateTime
        }
        Write-Warning "CI_PIPELINE_CREATED_AT='$created' is not a parseable timestamp; falling back to now."
    }

    return (Get-Date)
}

function Get-CiPipelineDateStamp {
    <#
    .SYNOPSIS
      Pipeline date as yyyy_MM_dd — the dated share/wheel folder leaf.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param()
    return (Get-CiPipelineDate).ToString('yyyy_MM_dd')
}

function Get-CiPipelineReportDateIso {
    <#
    .SYNOPSIS
      Pipeline date as yyyy-MM-dd — the triage ?date= / zip-name / PYTORCH_WIN_TEST_REPORT_DATE form.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param()
    return (Get-CiPipelineDate).ToString('yyyy-MM-dd')
}
