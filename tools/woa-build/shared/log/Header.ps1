#Requires -Version 5.1
<#
.SYNOPSIS
  Write-CiSubsetHeader — flow-start banner emitting the resolved subset + caller-supplied context.

.DESCRIPTION
  Carved out of CiWorkflow.ps1. Three concerns lumped in one helper because they share output
  formatting and would otherwise force every caller to assemble the line by hand.

  Dependencies (dot-sourced):
    * ../env/Secrets.ps1   - Hide-CiSecretsInString
    * ../workflow/Subset.ps1 - Get-CiBuildFlowSubset
#>

. (Join-Path $PSScriptRoot '..' 'env' 'Secrets.ps1')
. (Join-Path $PSScriptRoot '..' 'workflow' 'Subset.ps1')

function Write-CiSubsetHeader {
    <#
    .SYNOPSIS
      Emit a structured banner at the top of a flow run: which job, which subset, which env
      knobs the operator pinned.

    .DESCRIPTION
      Lines are prefixed with [<Component>] so they group with the surrounding Write-CiPhase
      output when an operator greps the CI job log. -Extra keys are sorted for stable
      diffs across reruns. Empty / whitespace values render as '<empty>' so a missing override
      is visually distinct from a present-but-blank one.

    .PARAMETER Component
      Log-component tag (matches Write-CiPhase -Component).

    .PARAMETER Extra
      Hashtable of caller-supplied context (CI_PROJECT_DIR, CHECKOUT_ROOT, EXT, ...).

    .NOTES
      Header.ps1 is the second of two carved-out Write-Host call-sites under shared/
      (see Phase.ps1 for the other). The strict PSScriptAnalyzer profile enforces
      PSAvoidUsingWriteHost on everything else.
    #>
    [CmdletBinding()]
    [Diagnostics.CodeAnalysis.SuppressMessageAttribute(
        'PSAvoidUsingWriteHost', '',
        Justification = 'Header.ps1 is a dedicated structured logger; carve-out documented in the strict lint profile.'
    )]
    param(
        [Parameter(Mandatory)][string] $Component,
        [hashtable] $Extra = @{}
    )

    Write-Host ("[{0}] === flow header ===" -f $Component)
    try {
        $subset = Get-CiBuildFlowSubset
        Write-Host ("[{0}]   build_flow_subset={1}" -f $Component, $subset)
    }
    catch {
        Write-Host ("[{0}]   build_flow_subset=<unresolved: {1}>" -f $Component, $_.Exception.Message)
    }

    foreach ($key in ($Extra.Keys | Sort-Object)) {
        $val = [string]$Extra[$key]
        if ([string]::IsNullOrWhiteSpace($val)) { $val = '<empty>' }
        Write-Host ("[{0}]   {1}={2}" -f $Component, $key, (Hide-CiSecretsInString -Text $val))
    }
}
