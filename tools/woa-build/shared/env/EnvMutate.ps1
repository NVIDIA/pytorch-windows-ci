#Requires -Version 5.1
<#
.SYNOPSIS
  Set-CiEnv / Remove-CiEnv / Add-EnvPathSegment / Join-EnvFlagFragment / Test-FlagTokenRunPresent —
  env mutation helpers carved out of CiCommon.ps1.

.DESCRIPTION
  Wraps every CI env write so secret tracking and the manifest contract are honoured uniformly.
  Path-style (PATH, CMAKE_PREFIX_PATH) and flag-style (CL, CMAKE_CUDA_FLAGS) helpers provide
  idempotent prepend / append / dedupe semantics.

  Dependencies:
    * Secrets.ps1     - Register-CiSecret (used by Set-CiEnv when secret-tracking active)
    * EnvManifest.ps1 - Get-CiEnvManifestEntry (used by Set-CiEnv to auto-detect Secret = $true)
#>

. (Join-Path $PSScriptRoot 'Secrets.ps1')
. (Join-Path $PSScriptRoot 'EnvManifest.ps1')

function Set-CiEnv {
    <#
    .SYNOPSIS
      Write a value to a process env var with consistent secret tracking and manifest awareness.

    .DESCRIPTION
      Wraps [Environment]::SetEnvironmentVariable so every CI mutation flows through one helper.
      Behaviour:
        * Auto-registers the value as a secret when -Secret is passed OR the manifest entry
          declares Secret = $true. This is the only thing standing between an
          operator-supplied token and a leaked job log when callers compute a value from
          something other than Resolve-CiEnv (e.g. concatenation, transform, fresh literal).
        * Empty/whitespace values are written verbatim. On Windows, writing '' via
          [Environment]::SetEnvironmentVariable actually unsets the slot — use Remove-CiEnv
          if you want explicit-unset semantics.
        * Returns the value written so callers can chain.

      Prefer this over bare `$env:X = Y` in any new code so we keep a single audit surface
      for CI env mutation.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $Name,
        [Parameter(Mandatory)][AllowEmptyString()][string] $Value,
        [switch] $Secret
    )

    $isSecret = $Secret.IsPresent
    if (-not $isSecret) {
        try {
            $entry = Get-CiEnvManifestEntry -Name $Name
            if ($null -ne $entry -and $true -eq $entry.Secret) { $isSecret = $true }
        }
        catch {
            # Best-effort manifest lookup — if the manifest isn't loaded yet or
            # the name is unknown, fall through and treat the value as non-secret.
            Write-Verbose ("Set-CiEnv: manifest lookup failed for '$Name': $($_.Exception.Message)")
        }
    }

    if ($PSCmdlet.ShouldProcess("Env:$Name", "set value (length=$($Value.Length))")) {
        [Environment]::SetEnvironmentVariable($Name, $Value, 'Process')
    }
    if ($isSecret -and -not [string]::IsNullOrEmpty($Value)) {
        Register-CiSecret -Value $Value
    }
    return $Value
}

function Remove-CiEnv {
    <#
    .SYNOPSIS
      Explicitly unset a process env var so downstream readers see "missing", not "empty".

    .DESCRIPTION
      Counterpart to Set-CiEnv. Calls [Environment]::SetEnvironmentVariable($Name, $null,
      'Process') which removes the variable from the process env block on every supported
      platform. Idempotent — removing an unset var is a no-op. Honours -WhatIf / -Confirm.

      Use this when you need to make sure subsequent code paths take the "not set" branch
      (e.g. clearing a credential after use, undoing a per-stage override). Plain
      `Set-CiEnv -Name X -Value ''` works on Windows but is ambiguous on cross-platform
      readers; this helper is the unambiguous form.
    #>
    [CmdletBinding(SupportsShouldProcess)]
    param([Parameter(Mandatory)][string] $Name)

    if ($PSCmdlet.ShouldProcess("Env:$Name", 'remove')) {
        [Environment]::SetEnvironmentVariable($Name, $null, 'Process')
    }
}

function Add-EnvPathSegment {
    <#
    .SYNOPSIS
      Idempotently prepend (default) or append segments to a path-style env var.

    .DESCRIPTION
      Splits the current value on -Separator (default ';'), merges -Segment in the requested
      direction, and dedupes while preserving first-occurrence order. The process env var is
      overwritten only when the resulting value would actually differ; the new value is always
      returned for diagnostics.

      Dedupe is case-insensitive (OrdinalIgnoreCase) and trailing-separator-insensitive, i.e.
      'C:\foo', 'C:\foo\', 'c:\FOO\' are all considered the same path. First-occurrence display
      casing/form is preserved.

      Use this for PATH, CMAKE_PREFIX_PATH, and any other separator-delimited path env var that
      build bootstrappers may mutate more than once in the same shell (retries, re-imports of
      the build env, test reruns). Repeated calls with the same segments are a true no-op —
      the env var does not grow and is not re-written.

      Empty/whitespace segments are dropped silently.

    .PARAMETER Name
      Env var name to mutate (e.g. 'PATH', 'CMAKE_PREFIX_PATH').

    .PARAMETER Segment
      One or more segments to add. Whitespace-only entries are ignored.

    .PARAMETER Separator
      Separator between segments. Defaults to ';' (Windows path-style).

    .PARAMETER Append
      Add segments at the end instead of the front. Order of -Segment is preserved.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $Name,

        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [AllowEmptyString()]
        [string[]] $Segment,

        [string] $Separator = ';',
        [switch] $Append
    )

    # [List[string]] keeps Add() at amortised O(1); a `+= $s` would rebuild the array on every
    # step (O(N^2)) which gets noticeable for long PATHs that collected many segments across
    # nested helpers.
    $clean = [System.Collections.Generic.List[string]]::new()
    foreach ($s in $Segment) {
        if (-not [string]::IsNullOrWhiteSpace($s)) {
            $clean.Add($s.Trim()) | Out-Null
        }
    }

    $current  = [Environment]::GetEnvironmentVariable($Name, 'Process')
    $existing = [System.Collections.Generic.List[string]]::new()
    if (-not [string]::IsNullOrWhiteSpace($current)) {
        foreach ($raw in $current.Split($Separator, [System.StringSplitOptions]::RemoveEmptyEntries)) {
            $trimmed = $raw.Trim()
            if (-not [string]::IsNullOrWhiteSpace($trimmed)) {
                $existing.Add($trimmed) | Out-Null
            }
        }
    }

    if ($clean.Count -eq 0) {
        return ($existing -join $Separator)
    }

    $ordered = [System.Collections.Generic.List[string]]::new($existing.Count + $clean.Count)
    if ($Append) {
        $ordered.AddRange($existing)
        $ordered.AddRange($clean)
    }
    else {
        $ordered.AddRange($clean)
        $ordered.AddRange($existing)
    }

    # Normalize trailing separators for dedupe identity, but keep the original form for output.
    # 'C:\foo' and 'C:\foo\' map to the same identity ('C:\foo'); the first occurrence wins on
    # display so the resulting env value stays close to what callers passed in.
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $final = foreach ($s in $ordered) {
        $identity = $s.TrimEnd('\', '/')
        if ([string]::IsNullOrEmpty($identity)) {
            continue
        }
        if ($seen.Add($identity)) { $s }
    }

    $value = $final -join $Separator
    if ($value -ne $current) {
        [Environment]::SetEnvironmentVariable($Name, $value, 'Process')
    }
    return $value
}

function Test-FlagTokenRunPresent {
    <#
    .SYNOPSIS
      Internal: is the ordered sequence of -Needle tokens present as a contiguous run inside
      -Haystack tokens (ordinal compare)?

    .DESCRIPTION
      Used by Join-EnvFlagFragment to decide whether the fragment is already in the env value.
      Token-level match (not substring) so '/Zc:preprocessor' does not match '/Zc:preprocessor20'
      and '-O2' does not match '-O20'.
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]] $Haystack,
        [Parameter(Mandatory)][AllowEmptyCollection()][string[]] $Needle
    )
    if ($Needle.Count -eq 0)             { return $true }
    if ($Needle.Count -gt $Haystack.Count) { return $false }

    $maxStart = $Haystack.Count - $Needle.Count
    for ($i = 0; $i -le $maxStart; $i++) {
        $matched = $true
        for ($j = 0; $j -lt $Needle.Count; $j++) {
            if (-not [string]::Equals($Haystack[$i + $j], $Needle[$j], [System.StringComparison]::Ordinal)) {
                $matched = $false
                break
            }
        }
        if ($matched) { return $true }
    }
    return $false
}

function Join-EnvFlagFragment {
    <#
    .SYNOPSIS
      Idempotently append a flag fragment to a flags-style env var (CL, CMAKE_CUDA_FLAGS, etc.).

    .DESCRIPTION
      Use this for env vars whose convention is "build systems append additional flags":
        * If the env var is unset/empty: set it to -Fragment.
        * If the env var already contains -Fragment as a contiguous run of whitespace-separated
          tokens (ordinal compare): leave it untouched.
        * Otherwise: write "<existing> <Fragment>" — preserving any operator-supplied flags.

      Token-aware match (not substring) so a longer existing flag that happens to share a prefix
      with -Fragment does not cause a false-positive skip. E.g. existing '/Zc:preprocessor20' and
      -Fragment '/Zc:preprocessor' are correctly seen as distinct.

      Crucially, this respects operator-set values. Doing `$env:CL = "<our flags>"` directly
      clobbers any `CL=/D MY_DEFINE` the caller had pre-set; this helper preserves it.

      Re-invocation in the same shell is a true no-op once -Fragment is present — the env var is
      not re-written and the value does not grow.

      Returns the resulting env value for diagnostic / logging use.

    .PARAMETER Name
      Env var name to mutate (e.g. 'CL', 'CMAKE_CUDA_FLAGS', 'LDFLAGS').

    .PARAMETER Fragment
      Flag fragment to append. Whitespace-only fragments are a no-op. Multi-token fragments
      (e.g. '-Xcompiler /Zc:preprocessor') are matched as an ordered contiguous run.

    .PARAMETER Separator
      String inserted between the existing value and -Fragment. Defaults to a single space, which
      matches every compiler/linker flag env var in common use.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $Name,

        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string] $Fragment,

        [string] $Separator = ' '
    )

    $current = [Environment]::GetEnvironmentVariable($Name, 'Process')

    if ([string]::IsNullOrWhiteSpace($Fragment)) {
        if ($null -eq $current) { return '' }
        return $current
    }
    if ([string]::IsNullOrWhiteSpace($current)) {
        [Environment]::SetEnvironmentVariable($Name, $Fragment, 'Process')
        return $Fragment
    }

    $existingTokens = @($current -split '\s+' | Where-Object { -not [string]::IsNullOrEmpty($_) })
    $fragmentTokens = @($Fragment -split '\s+' | Where-Object { -not [string]::IsNullOrEmpty($_) })
    if (Test-FlagTokenRunPresent -Haystack $existingTokens -Needle $fragmentTokens) {
        return $current
    }

    $merged = "{0}{1}{2}" -f $current, $Separator, $Fragment
    if ($merged -ne $current) {
        [Environment]::SetEnvironmentVariable($Name, $merged, 'Process')
    }
    return $merged
}
