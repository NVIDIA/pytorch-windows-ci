# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Get-VariantLogSuffix — '_<VARIANT_NAME>' suffix for log filenames.

.DESCRIPTION
  The runners reuse the same working directory between sequential jobs
  (with CHECKOUT_REUSE_EXISTING:true + GIT_CLEAN_FLAGS:none, which the PyTorch
  build pipeline sets explicitly). Without a per-variant suffix, ctk134's
  pip-wheel.log silently overwrites ctk131's on disk before the artifact
  uploader has a chance to capture it, and even when artifacts ARE per-job the
  identical filename makes them indistinguishable after `unzip` into one dir.

  Suffixing with VARIANT_NAME keeps each variant's logs uniquely named both on
  disk and in the downloaded artifact bundle. This helper is used by every log
  file that is written under $logsDir from inside a per-variant job:

    * torch/WheelPipeline.ps1  (pip-wheel{suffix}.log, pip-wheel-cuda-embed{suffix}.log)
    * shared/build/ExtensionBuildPipeline.ps1  (pip-wheel-<ext>-cuda-embed-torch{suffix}.log)
    * torchvision/Build.ps1    (delvewheel-torchvision{suffix}.log)

  Returns the empty string when VARIANT_NAME is unset (single-variant or
  local-dev runs) so the original filename is preserved untouched.

  No dependencies — safe to dot-source standalone.
#>

function Get-VariantLogSuffix {
    [CmdletBinding()]
    [OutputType([string])]
    param()

    $variant = $env:VARIANT_NAME
    if ([string]::IsNullOrWhiteSpace($variant)) { return '' }

    # Sanitize to filename-safe chars — variant names are already `[A-Za-z0-9_]+`
    # per ci/templates/pytorch-windows-variants.yml, but a user override should
    # not break filesystem ops.
    return '_' + ($variant -replace '[^A-Za-z0-9_\-]', '_')
}
