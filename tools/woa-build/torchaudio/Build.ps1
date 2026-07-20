# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

<#
.SYNOPSIS
  Build torchaudio Windows wheels against the CUDA-embedded PyTorch wheel.

.DESCRIPTION
  Thin caller around Invoke-PytorchExtensionBuild (shared with torchvision). All common steps
  (workdir, venv, torch install, vcvars, env wiring, pip wheel + verification) live in
  ci/scripts/windows/shared/build/ExtensionBuildPipeline.ps1.

.NOTES
  Logs under CI_PROJECT_DIR/logs (pip-wheel-torchaudio-*.log). Grep job logs for `phase=torchaudio_`.
  Long clone paths: EXTENSION_WIN_WORK_PARENT (short base dir); git uses core.longpaths=true on clone.
#>

. (Join-Path $PSScriptRoot '..\shared\build\ExtensionBuildPipeline.ps1')
. (Join-Path $PSScriptRoot '..\shared\env\EnvDefaults.ps1')

function Invoke-TorchaudioWindowsBuild {
    <#
    .SYNOPSIS
      Run the full torchaudio extension wheel pipeline.
    #>
    Invoke-PytorchExtensionBuild `
        -ExtName 'torchaudio' `
        -LocalDirectoryName 'audio' `
        -PackageName 'torchaudio' `
        -VenvName 'venv_build_audio'
}
