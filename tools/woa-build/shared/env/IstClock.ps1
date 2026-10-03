# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  IST (UTC+05:30) clock - the single zone every pipeline timestamp is rendered in.

.DESCRIPTION
  Dependency-free by design, so leaf scripts that only need a timestamp can dot-source this
  without pulling in the rest of shared/env/.

  Fixed offset rather than [TimeZoneInfo]: India has no DST, and the Windows id
  ('India Standard Time') differs from the IANA one ('Asia/Kolkata'). A constant behaves
  identically under Windows PowerShell 5.1, pwsh 7, and the `date` arithmetic in
  publish-test-reports.sh.

  ConvertTo-CiIstTime is the boundary converter: the filesystem hands out mtimes in UTC (that is
  what NTFS stores), so those are converted on read and no non-IST value reaches any comparison.

  Two callers, two meanings:
    * PipelineDate.ps1 pins the pipeline DATE to CI_PIPELINE_CREATED_AT, so every job agrees.
    * Get-CiIstTimestamp is an event TIMESTAMP - the moment a thing actually happened.
#>

$Script:CiIstOffsetMinutes = 330

function Get-CiIstOffset {
    <#
    .SYNOPSIS
      IST's fixed UTC offset (+05:30).
    #>
    [CmdletBinding()]
    [OutputType([TimeSpan])]
    param()
    return [TimeSpan]::FromMinutes($Script:CiIstOffsetMinutes)
}

function ConvertTo-CiIst {
    <#
    .SYNOPSIS
      Re-render an instant in IST. Same moment, different offset.

    .PARAMETER Instant
      Typically a parsed CI_PIPELINE_CREATED_AT or [DateTimeOffset]::UtcNow.
    #>
    [CmdletBinding()]
    [OutputType([DateTimeOffset])]
    param([Parameter(Mandatory)][DateTimeOffset] $Instant)
    return $Instant.ToOffset((Get-CiIstOffset))
}

function ConvertTo-CiIstTime {
    <#
    .SYNOPSIS
      An instant from outside the pipeline (e.g. FileInfo.LastWriteTimeUtc) as an IST wall-clock
      [datetime], directly comparable with (Get-CiIstNow).DateTime.

    .PARAMETER Instant
      A [datetime] whose Kind says which zone it is in. Utc (filesystem timestamps) and Local are
      both fine - .NET knows the offset for each. Unspecified is rejected: .NET would assume local,
      which silently misconverts a value that was really UTC. Note this function's own output is
      Unspecified ([datetime] cannot encode +05:30), so the guard also stops it being fed back in
      and shifted twice.
    #>
    [CmdletBinding()]
    [OutputType([datetime])]
    param([Parameter(Mandatory, Position = 0)][datetime] $Instant)
    if ($Instant.Kind -eq [DateTimeKind]::Unspecified) {
        throw ("ConvertTo-CiIstTime: -Instant has Kind=Unspecified, so its source zone is unknown " +
            "and .NET would assume local. Pass a Utc value (e.g. FileInfo.LastWriteTimeUtc) or a " +
            "Local one, or tag it with [datetime]::SpecifyKind(<value>, 'Utc').")
    }
    return (ConvertTo-CiIst -Instant ([DateTimeOffset]$Instant)).DateTime
}

function Get-CiIstNow {
    <#
    .SYNOPSIS
      Now, in IST. Derived from UtcNow, so it is identical on a runner in any timezone.
    #>
    [CmdletBinding()]
    [OutputType([DateTimeOffset])]
    param()
    return (ConvertTo-CiIst -Instant ([DateTimeOffset]::UtcNow))
}

function Get-CiIstTimestamp {
    <#
    .SYNOPSIS
      Now as ISO-8601 with an explicit +05:30 suffix ('2026-08-31T14:22:07.1234567+05:30') - the
      form written into JSON audit records, so readers can recover the exact instant.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param()
    return (Get-CiIstNow).ToString('yyyy-MM-ddTHH:mm:ss.fffffffK',
        [System.Globalization.CultureInfo]::InvariantCulture)
}
