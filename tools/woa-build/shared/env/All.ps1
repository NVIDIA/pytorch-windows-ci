# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#Requires -Version 5.1
<#
.SYNOPSIS
  Convenience aggregator: dot-source the entire shared/env/ surface in one shot.

.DESCRIPTION
  Top-level flow scripts (pytorch-windows-build-flow.ps1, pytorch-windows-test-shard.ps1, ...)
  typically need every env helper. Rather than dot-sourcing six files individually they may
  dot-source this aggregator. Internal helpers should keep dot-sourcing only what they actually
  use to keep dependency arrows narrow.

  Loading order matches the dependency DAG so each file finds its deps already in scope:
    Secrets.ps1      -> standalone
    EnvSnapshot.ps1  -> standalone
    EnvManifest.ps1  -> standalone
    Defaults.ps1     -> standalone (populates $Script:CiDefaultsTable)
    EnvDefaults.ps1  -> Defaults.ps1                                 (re-loads cheap; idempotent)
    EnvResolve.ps1   -> Secrets.ps1, EnvManifest.ps1, EnvDefaults.ps1 (re-loads cheap; idempotent)
    EnvMutate.ps1    -> Secrets.ps1, EnvManifest.ps1                  (re-loads cheap; idempotent)
    PipelineDate.ps1 -> EnvResolve.ps1                               (re-loads cheap; idempotent)
#>

. (Join-Path $PSScriptRoot 'Secrets.ps1')
. (Join-Path $PSScriptRoot 'EnvSnapshot.ps1')
. (Join-Path $PSScriptRoot 'EnvManifest.ps1')
. (Join-Path $PSScriptRoot 'EnvDefaults.ps1')
. (Join-Path $PSScriptRoot 'EnvResolve.ps1')
. (Join-Path $PSScriptRoot 'EnvMutate.ps1')
. (Join-Path $PSScriptRoot 'PipelineDate.ps1')
