#Requires -Version 5.1
<#
.SYNOPSIS
  Write-CiPhase — structured single-line phase logger. Carved out of CiCommon.ps1.

.DESCRIPTION
  CI job triage matches on the `[<component>][<state>] phase=...` shape. Don't change the
  string without coordinating with the triage scripts.

  Dependencies (dot-sourced):
    * ../env/Secrets.ps1 - Hide-CiSecretsInString
#>

. (Join-Path $PSScriptRoot '..' 'env' 'Secrets.ps1')

function Write-CiPhase {
    <#
    .SYNOPSIS
      Emit a structured single-line phase log to the host stream so CI job logs and
      Select-String -Pattern '\[<state>\]' searches stay machine-greppable.

    .PARAMETER State
      One of START, PASS, FAIL, INFO, SKIP, WARN. Validated.

    .PARAMETER Phase
      Short identifier, snake_case (e.g. vcvars_resolve_path).

    .PARAMETER Detail
      Optional free-form trailing context. Rendered after ' | '.

    .PARAMETER Component
      Defaults to 'pytorch-windows-build-flow' to match existing log greps. Override per-script
      where useful (e.g. 'share-wheel-access').

    .NOTES
      This function is the ONE allowed Write-Host call-site under shared/. The strict
      PSScriptAnalyzer pass enforces PSAvoidUsingWriteHost on the rest of the tree; the
      suppression below is the carve-out the plan documents (see .PSScriptAnalyzerSettings.Strict.psd1).
    #>
    [CmdletBinding()]
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute(
        'PSAvoidUsingWriteHost', '',
        Justification = 'Phase.ps1 is the dedicated structured logger; the strict lint profile carves it out by design.'
    )]
    param(
        [Parameter(Mandatory)]
        [ValidateSet('START', 'PASS', 'FAIL', 'INFO', 'SKIP', 'WARN')]
        [string] $State,

        [Parameter(Mandatory)][string] $Phase,
        [string] $Detail = '',
        [string] $Component = 'pytorch-windows-build-flow'
    )

    $line = "[{0}][{1}] phase={2}" -f $Component, $State, $Phase
    if (-not [string]::IsNullOrWhiteSpace($Detail)) {
        $line = "$line | $Detail"
    }
    Write-Host (Hide-CiSecretsInString -Text $line)
}
