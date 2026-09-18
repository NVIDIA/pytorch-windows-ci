# CUDA - Windows on Arm release and package policy

This document distinguishes the cadence of this repository's CI from the
cadence of packages published to NVIDIA's stable and nightly Python indexes.
The CI repository builds and validates wheels, but it does not contain the
credentials or upload step that publishes those wheels to the package indexes.

## Stable release history

| PyTorch | TorchVision | TorchAudio | CUDA | Architecture | Publication date |
| --- | --- | --- | --- | --- | --- |
| 2.14.0 | 0.29.0 | 2.11.0 | 13.4 | Windows ARM64 | September 3, 2026 |

The package indexes are the source of truth for the files and Python versions
that remain available:

- [Stable PyTorch packages](https://pypi.nvidia.com/nvtorch_oot/torch/)
- [Stable TorchVision packages](https://pypi.nvidia.com/nvtorch_oot/torchvision/)
- [Stable TorchAudio packages](https://pypi.nvidia.com/nvtorch_oot/torchaudio/)

This repository has no GitHub release tags for the wheel versions. Repository
changes are delivered through pull requests to `main`; installable releases
are versioned in the NVIDIA package indexes.

## Stable release cadence

Stable CUDA - Windows on Arm package support begins with PyTorch 2.14. Stable
packages follow the upstream
[PyTorch release timeline](https://github.com/pytorch/pytorch/blob/main/RELEASE.md)
after platform qualification completes.

NVIDIA commits to maintaining CUDA - Windows on Arm for at least 12 months
after the platform is accepted for the PyTorch Additional Compute Platforms
program. Maintenance includes the public package installation path, nightly CI,
issue triage, compatibility documentation, and updates needed to keep supported
configurations usable during that period.

Stable qualification must include the advertised OS, Python, CUDA, driver, and
GPU configurations.

## Nightly cadence

The Windows-on-Arm source build and test workflow is scheduled once per day at
09:50 UTC. It resolves the newest upstream `pytorch/pytorch` nightly commit
available at the start of the run, builds PyTorch, TorchVision, and TorchAudio,
runs the configured test suite, and reports authoritative build and test
conclusions to the PyTorch HUD.

Nightly packages use a `devYYYYMMDD` version derived from the upstream nightly
commit. They are published on a best-effort basis. The separate NVIDIA
package-publication process is not implemented in this repository, so the daily
CI schedule does not guarantee that a new index entry will be published every
day.

- [Nightly PyTorch packages](https://pypi.nvidia.com/nvtorch_oot_nightly/torch/)
- [Nightly TorchVision packages](https://pypi.nvidia.com/nvtorch_oot_nightly/torchvision/)
- [Nightly TorchAudio packages](https://pypi.nvidia.com/nvtorch_oot_nightly/torchaudio/)
- [Windows-on-Arm workflow](../.github/workflows/windows-woa-build-test.yml)

## Retention and deprecation

GitHub Actions build wheels, test reports, and diagnostic artifacts are retained
for 14 days. This artifact lifetime supports CI debugging and is independent of
the installable packages on the NVIDIA indexes.

Stable packages beginning with PyTorch 2.14 and nightly packages are retained
on the NVIDIA package indexes for long-term availability. No routine short-term
expiration window is applied to either package channel.

If NVIDIA deprecates a stable or nightly package, Python version, CUDA version,
GPU architecture, or platform feature, a notice will be published at least
30 days before removal. Notices will be published in this repository and linked
from the relevant installation or compatibility documentation. The package
indexes remain the source of truth for files that are currently downloadable.
