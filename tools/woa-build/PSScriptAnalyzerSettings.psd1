# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: MIT

#
# PSScriptAnalyzer settings for the vendored WoA build/test library
# (consumed by .github/workflows/lint.yml `powershell` job via -Settings).
#
# We exclude rules that CI logging scripts intentionally and universally trip, so
# PR annotations focus on real issues rather than drowning in style noise on
# faithfully-vendored code:
#   * PSAvoidUsingWriteHost        - these scripts log progress to the job log by
#                                    design (Write-CiPhase / Write-Host).
#   * PSUseBOMForUnicodeEncodedFile - a UTF-8 BOM is undesirable in git / on
#                                    cross-platform runners.
#   * PSUseSingularNouns           - some vendored helpers use plural nouns;
#                                    renaming vendored functions would fight future
#                                    re-vendors for no runtime benefit.
#
# Everything else stays on. Errors fail the lint job; remaining warnings annotate.
#
@{
    ExcludeRules = @(
        'PSAvoidUsingWriteHost'
        'PSUseBOMForUnicodeEncodedFile'
        'PSUseSingularNouns'
    )
}
