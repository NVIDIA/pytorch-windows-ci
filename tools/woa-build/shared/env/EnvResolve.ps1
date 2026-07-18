#Requires -Version 5.1
<#
.SYNOPSIS
  Resolve-CiEnv / Test-CiEnvSet / Assert-RequiredCiEnv / Test-EnvTruthy — env reading helpers.

.DESCRIPTION
  Carved out of CiCommon.ps1. Reads through the case-insensitive process env block with
  manifest-driven defaults and secret tracking.

  Dependencies (dot-sourced):
    * Secrets.ps1     - Register-CiSecret
    * EnvManifest.ps1 - Get-CiEnvManifestEntry
    * EnvDefaults.ps1 - Get-CiDefault (transitively pulls Defaults.ps1 / the defaults table)
#>

. (Join-Path $PSScriptRoot 'Secrets.ps1')
. (Join-Path $PSScriptRoot 'EnvManifest.ps1')
. (Join-Path $PSScriptRoot 'EnvDefaults.ps1')

function Resolve-CiEnv {
    <#
    .SYNOPSIS
      Read a process env var with a case-insensitive fallback scan, falling through to a default.

    .DESCRIPTION
      CI can deliver CI/CD variable keys in unexpected casing (mostly lower-case) when the
      runner re-marshals them; Linux runners are case-sensitive. This helper does:
        1. Direct lookup of $Name on the process env block.
        2. Case-insensitive scan over the process env block.
        3. Return $Default (default '') if nothing matched.

      Default semantics (without -AllowEmpty): the env var is treated as "missing" when it is
      unset, empty, or whitespace-only, and $Default is returned. This is the historical
      contract every caller relies on.

      With -AllowEmpty: an explicit empty value set by the operator is honoured and returned as
      ''. Only an entirely unset variable falls back to $Default. Use this when a caller wants to
      allow operators to clear a default (e.g. CMAKE_PREFIX_PATH=) via a workflow-level env
      override, which cannot truly "unset" a CI/CD variable.

      Manifest integration (env/CiEnvManifest.psd1):
        * If the caller does NOT pass -Default and the manifest entry for $Name has a non-null
          DefaultKey, Get-CiDefault is used as the implicit default. Callers can therefore drop
          the redundant `-Default (Get-CiDefault X)` once the manifest is wired up.
        * If the manifest entry has Secret = $true, the resolved value is auto-registered with
          Register-CiSecret (same effect as passing -Secret). Defaults are NEVER registered as
          secrets.
        * Manifest lookup is best-effort: if the manifest cannot be loaded (early bootstrap,
          tests with private probe names) the legacy behaviour applies unchanged.

    .PARAMETER Name
      The CI/CD variable name as authored (e.g. PYTORCH_WIN_BUILD_VCVARS_BAT).

    .PARAMETER Default
      Value to return when the env var is unset (or whitespace-only without -AllowEmpty). When
      omitted, the resolver consults CiEnvManifest.DefaultKey and falls through to Get-CiDefault.
      Pass an explicit value (including '') to override the manifest-driven default.

    .PARAMETER AllowEmpty
      When set, an operator-supplied empty/whitespace value is returned as-is instead of falling
      through to $Default. Only a truly unset variable hits the default.

    .PARAMETER Secret
      When set, the resolved non-empty value is registered with Register-CiSecret so subsequent
      Write-CiPhase / Hide-CiSecretsInString calls redact it. Use for tokens, passwords, and any
      value an operator would not want echoed in the CI job logs. Auto-applied when the manifest
      entry declares Secret = $true.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $Name,
        [string] $Default = '',
        [switch] $AllowEmpty,
        [switch] $Secret
    )

    $callerSuppliedDefault = $PSBoundParameters.ContainsKey('Default')

    # Manifest is best-effort. Failures (no manifest file, defaults not yet loaded, etc.) must
    # never break the legacy contract.
    $manifestEntry = $null
    try { $manifestEntry = Get-CiEnvManifestEntry -Name $Name } catch { $manifestEntry = $null }

    if (-not $callerSuppliedDefault -and $null -ne $manifestEntry -and
        -not [string]::IsNullOrWhiteSpace([string]$manifestEntry.DefaultKey)) {
        try { $Default = Get-CiDefault -Name ([string]$manifestEntry.DefaultKey) }
        catch { Write-Verbose "Resolve-CiEnv: manifest DefaultKey '$($manifestEntry.DefaultKey)' for '$Name' not resolvable: $_" }
    }

    $secretActive = $Secret.IsPresent -or
        ($null -ne $manifestEntry -and $true -eq $manifestEntry.Secret)

    # Local helper to register the resolved value as a secret when secret-handling is active and
    # the value is non-empty. Keeps the return paths tidy without sprinkling Register-CiSecret
    # everywhere. Defaults are deliberately NOT registered (a default is, by definition, public).
    $registerIfSecret = {
        param([string] $val)
        if ($secretActive -and -not [string]::IsNullOrEmpty($val)) {
            Register-CiSecret -Value $val
        }
        return $val
    }

    $direct = [Environment]::GetEnvironmentVariable($Name, 'Process')
    if ($AllowEmpty) {
        if ($null -ne $direct) {
            return (& $registerIfSecret $direct)
        }
    }
    elseif (-not [string]::IsNullOrWhiteSpace($direct)) {
        return (& $registerIfSecret $direct)
    }

    # Sort keys ordinally before the case-insensitive scan so that when two keys differ only by
    # case (legal on Linux runners, possible on Windows via P/Invoke) the same key always wins
    # across runs, locales and CLR versions. We use [Array]::Sort with StringComparer.Ordinal
    # rather than Sort-Object so the order is fully culture-independent (Sort-Object honours the
    # current culture even with -CaseSensitive).
    $procEnv = [Environment]::GetEnvironmentVariables('Process')
    $orderedKeys = @($procEnv.Keys | ForEach-Object { [string]$_ })
    [System.Array]::Sort($orderedKeys, [System.StringComparer]::Ordinal)
    foreach ($key in $orderedKeys) {
        if ([string]::Equals([string]$key, $Name, [System.StringComparison]::OrdinalIgnoreCase)) {
            $candidate = $procEnv[$key]
            if ($AllowEmpty) {
                if ($null -ne $candidate) {
                    return (& $registerIfSecret ([string]$candidate))
                }
            }
            elseif (-not [string]::IsNullOrWhiteSpace([string]$candidate)) {
                return (& $registerIfSecret ([string]$candidate))
            }
        }
    }

    return $Default
}

function Test-CiEnvSet {
    <#
    .SYNOPSIS
      $true when the named CI/CD env var resolves to a non-whitespace value (case-insensitive).

    .DESCRIPTION
      Convenience predicate over Resolve-CiEnv that avoids the
      `if ([string]::IsNullOrWhiteSpace((Resolve-CiEnv -Name 'X'))) { ... }` boilerplate.
      Defaults are deliberately ignored here — the question is "did the operator set this?",
      not "what value will downstream code see?".
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param([Parameter(Mandatory)][string] $Name)
    $val = Resolve-CiEnv -Name $Name -Default ''
    return -not [string]::IsNullOrWhiteSpace($val)
}

function Assert-RequiredCiEnv {
    <#
    .SYNOPSIS
      Resolve a required env var; throw a clear, actionable error when missing.

    .DESCRIPTION
      Use at job/script entry points to fail fast when an operator-required CI/CD variable is
      absent. Honours all Resolve-CiEnv semantics (case-insensitive scan, manifest-driven
      DefaultKey + Secret) and additionally throws when the resolved value is empty or
      whitespace.

      Intended pattern:
          $projectDir = Assert-RequiredCiEnv -Name 'CI_PROJECT_DIR'
          $shard      = Assert-RequiredCiEnv -Name 'PYTORCH_CI_TEST_SHARD'

    .PARAMETER Name
      The CI/CD variable name. If it is declared in CiEnvManifest.psd1, the manifest's
      Description is included in the error message to help the operator.

    .PARAMETER Default
      Optional fallback. If supplied, takes the place of the manifest-driven default.

    .PARAMETER Secret
      Forwarded to Resolve-CiEnv (also auto-applied when the manifest declares Secret = $true).
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param(
        [Parameter(Mandatory)][string] $Name,
        [string] $Default,
        [switch] $Secret
    )

    $resolveArgs = @{ Name = $Name }
    if ($PSBoundParameters.ContainsKey('Default')) { $resolveArgs['Default'] = $Default }
    if ($Secret) { $resolveArgs['Secret'] = $true }

    $value = Resolve-CiEnv @resolveArgs
    if ([string]::IsNullOrWhiteSpace($value)) {
        $hint = ''
        try {
            $entry = Get-CiEnvManifestEntry -Name $Name
            if ($null -ne $entry -and -not [string]::IsNullOrWhiteSpace([string]$entry.Description)) {
                $hint = " ($($entry.Description))"
            }
        }
        catch {
            # Best-effort manifest lookup for the human-readable hint — the
            # outer throw below is the real error contract.
            Write-Verbose ("Assert-RequiredCiEnv: manifest lookup failed for '$Name': $($_.Exception.Message)")
        }
        throw "Required CI env var '$Name' is not set.$hint"
    }
    return $value
}

function Test-EnvTruthy {
    <#
    .SYNOPSIS
      Return $true when the env var (looked up via Resolve-CiEnv) holds a canonical truthy value.

    .DESCRIPTION
      Truthy values are the lowercase set {'1', 'true', 'yes'}. Anything else, including '0',
      'false', 'no', or a free-form string, is $false.

      -Default is forwarded to Resolve-CiEnv so a feature whose "default on" state lives in
      Defaults.ps1 can be represented faithfully (e.g. Test-EnvTruthy -Name 'FOO' -Default '1'
      reports $true when the env var is unset).

    .PARAMETER Name
      The CI/CD variable name.

    .PARAMETER Default
      Value to evaluate when the env var is unset. Defaults to '' (i.e. $false).
    #>
    [CmdletBinding()]
    [OutputType([bool])]
    param(
        [Parameter(Mandatory)][string] $Name,
        [string] $Default = ''
    )

    $val = Resolve-CiEnv -Name $Name -Default $Default
    if ([string]::IsNullOrWhiteSpace($val)) {
        return $false
    }
    return @('1', 'true', 'yes') -contains $val.Trim().ToLowerInvariant()
}
