#Requires -Version 5.1
<#
.SYNOPSIS
  Allow-list of resolved values that downstream loggers must redact before emission.

.DESCRIPTION
  Carved out of the former monolithic CiCommon.ps1. Registration is deliberately opt-in:
  callers tag a value as secret via Register-CiSecret or via Resolve-CiEnv -Secret.

  No dependencies; safe to dot-source standalone.
#>

if ($null -eq $Script:CiSecretValues) {
    $Script:CiSecretValues = [System.Collections.Generic.HashSet[string]]::new(
        [System.StringComparer]::Ordinal
    )
}

function Register-CiSecret {
    <#
    .SYNOPSIS
      Register a literal value as a secret so Hide-CiSecretsInString will redact it from logs.

    .DESCRIPTION
      Idempotent. Empty/whitespace values are ignored (no point redacting '').
      Values are stored ordinal so case-sensitivity is preserved (tokens are case-sensitive).
    #>
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Value)
    if (-not [string]::IsNullOrWhiteSpace($Value)) {
        [void]$Script:CiSecretValues.Add($Value)
    }
}

function Hide-CiSecretsInString {
    <#
    .SYNOPSIS
      Replace any registered secret literal inside the input string with '[REDACTED]'.

    .DESCRIPTION
      Performs ordinal substring replacement for every registered secret. The mask string is
      '[REDACTED]'. Returns the input unchanged when no secrets are registered or when the
      input is null/empty.
    #>
    [CmdletBinding()]
    [OutputType([string])]
    param([Parameter(Mandatory)][AllowEmptyString()][string] $Text)
    if ([string]::IsNullOrEmpty($Text))          { return $Text }
    if ($Script:CiSecretValues.Count -eq 0)      { return $Text }

    $masked = $Text
    foreach ($secret in $Script:CiSecretValues) {
        if ([string]::IsNullOrEmpty($secret)) { continue }
        $masked = $masked.Replace($secret, '[REDACTED]')
    }
    return $masked
}

function Clear-CiSecrets {
    <#
    .SYNOPSIS
      Forget all registered secret literals. Primarily for Pester test cleanup.
    #>
    [CmdletBinding()]
    param()
    $Script:CiSecretValues.Clear()
}
