<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: MIT
-->

# PyTorch OOT Windows CI

Out-of-tree (OOT) GitHub Actions CI that builds and tests
[`pytorch/pytorch`](https://github.com/pytorch/pytorch) on NVIDIA's self-hosted
**Windows + RTX** (x86-64) and **Windows-on-Arm (WoA)** runners, across multiple
Python and CUDA toolkit combinations.

# Overview

This repository hosts GitHub Actions workflows that build and test PyTorch on
NVIDIA's Windows runners. It implements the downstream half of
[RFC-0050: Cross-Repository CI Relay for PyTorch Out-of-Tree Backends](https://github.com/pytorch/rfcs/blob/master/RFC-0050-Cross-Repository-CI-Relay-for-PyTorch-Out-of-Tree-Backends.md).

This repo expands the support matrix to catch regressions across multiple Python and
CUDA toolkit combinations before they show up upstream. PyTorch's in-tree
`.ci/pytorch/*.sh` scripts perform the builds and tests; this repository
provides workflow wiring, runner selection, diagnostics, and cross-repository
relay integration. Build and test jobs run on self-hosted NVIDIA runners;
lightweight jobs such as lint, prep/ref-resolution, and test-summary run on
GitHub-hosted `ubuntu-latest`.

> **Full architecture, matrix, and runner model:**
> [docs/ci-details.md](docs/ci-details.md).

> **Windows-on-Arm (WoA) CI:** see [docs/woa-ci.md](docs/woa-ci.md) for the
> arm64 build/test matrix, runner contract, and operator guide.

# Getting Started

The scheduled workflows need no action to run — see [Usage](#usage) for the
trigger model. The sections below cover consuming a published wheel and
building locally.

## Install built wheels

NVIDIA publishes stable and nightly PyTorch, TorchVision, and TorchAudio wheels
built by this CI.

> **Architecture:** the NVIDIA indexes below currently publish **Windows arm64
> (`win_arm64`) wheels only**. On Windows x86-64, install from
> [download.pytorch.org](https://pytorch.org/get-started/locally/) instead, or
> build from source using the
> [x86_64 build guide](docs/build_pytorch_windows_x86_64.md).

### Windows ARM64 compatibility

The published-wheel and CI coverage are intentionally distinguished below. A
wheel being present in an index means that configuration is available to
install; the **CI-tested** row identifies the configuration exercised by the
current nightly workflow.

| Component | Windows ARM64 coverage |
| --- | --- |
| Operating system | Windows 11 on Arm, version 24H2 or later (`win_arm64`). |
| Host architecture | Native ARM64. x64-emulated Python is not supported for source builds. |
| NVIDIA GPU | An NVIDIA GPU supported by CUDA 13.4 is required. Wheels are built for CUDA architectures `8.9`, `10.3+PTX`, `12.0`, and `12.1+PTX`. |
| NVIDIA driver | NVIDIA R615 or later is required. |
| CUDA | CUDA 13.4. Published wheels embed the required ARM64 CUDA runtime libraries; a compatible NVIDIA driver is still required. |
| cuDNN | cuDNN 9.25 ARM64 runtime libraries are embedded in the published wheel. |
| Python packages | The stable and nightly package indexes below are the source of truth for the Python versions that currently have `win_arm64` wheels. |
| CI-tested configuration | The current Windows-on-Arm nightly workflow builds and tests Python 3.13 with CUDA 13.4. |
| Stable framework versions | PyTorch 2.14.0, TorchVision 0.29.0, and TorchAudio 2.11.0 with CUDA 13.4. |
| Nightly framework versions | PyTorch, TorchVision, and TorchAudio pre-release wheels are published to the nightly index below. |

The CUDA architecture list describes what is compiled into the wheel; it is
not a claim that every corresponding GPU model is present in the nightly test
pool. See the [Windows-on-Arm CI guide](docs/woa-ci.md) for the exact CI matrix
and the [Windows ARM64 build guide](docs/build_pytorch_windows_arm64.md) for
toolchain and source-build requirements.

See the [CUDA - Windows on Arm platform support](docs/platform-support.md)
document for upstream integration, supported APIs, and disabled or untested
functionality.

See the [release and package policy](docs/release-policy.md) for release history,
CI and package cadence, artifact retention, and the package-policy commitments
that still require release-owner confirmation.

### Stable release

Install the CUDA 13.4 stable release from the NVIDIA stable index:

```bash
python -m pip install "torch==2.14.0+cu134" "torchvision==0.29.0+cu134" "torchaudio==2.11.0+cu134" --extra-index-url https://pypi.nvidia.com/nvtorch_oot/
```

Before installing, confirm that wheels for the required Python version and
architecture are available in the stable indexes for
[PyTorch](https://pypi.nvidia.com/nvtorch_oot/torch/),
[TorchVision](https://pypi.nvidia.com/nvtorch_oot/torchvision/), and
[TorchAudio](https://pypi.nvidia.com/nvtorch_oot/torchaudio/).

### Nightly

Install the latest pre-release wheels from the NVIDIA nightly index:

```bash
python -m pip install --pre torch torchvision torchaudio --extra-index-url https://pypi.nvidia.com/nvtorch_oot_nightly/
```

`--pre` enables nightly versions. NVIDIA indexes are supplied as extra indexes
so dependencies can still resolve from the default Python Package Index. The
active Python version, operating system, architecture, and CUDA compatibility
determine which wheel `pip` selects.

Verify the installation:

```bash
python -c "import torch; print(torch.__version__); print(torch.version.cuda); print(torch.cuda.is_available())"
```

## Build locally

Use the guide for the target architecture:

- [Windows x86_64 with CUDA](docs/build_pytorch_windows_x86_64.md)
- [Windows ARM64 with CUDA](docs/build_pytorch_windows_arm64.md)

Each guide covers its compiler, Python environment, CUDA dependencies, build
variables, build command, installation, and verification. The ARM64 guide also
covers the required CUDA-library overrides and wheel repacking with ARM64 DLLs.

For the repository's WoA CI workflow rather than a local build, see the
[WoA operator guide](docs/woa-ci.md) and
[WoA design and runner contract](docs/woa-ci-plan.md).

Both nightly workflows also publish their results to the upstream PyTorch HUD
through the Cross-Repo CI Relay — see
[HUD reporting](docs/crcr-hud-reporting.md).

# Requirements

- Self-hosted Windows runners from NVIDIA infrastructure, labelled for the
  build/test pools (`rtx-build`, `rtx-40x0-test`, `rtx-50x0-test`) and tagged per
  Python/CUDA cell (`py3xx`, `cu1xx`).
- Ephemeral, pre-prepped runner images carrying Python, the CUDA toolkit +
  driver, MSVC build tools, and the PyTorch test runtime — the workflows do zero
  in-job setup. See [docs/ci-details.md](docs/ci-details.md) for the full image
  contents and label routing.
- Windows-on-Arm (arm64) runners share a single persistent pool labelled
  `woa-arm64` for both build and test, with the toolchain preinstalled and a
  clean per-job venv built in-job. See [docs/woa-ci.md](docs/woa-ci.md) for the
  runner contract.

Local build prerequisites are listed in each architecture-specific build guide.

# Usage

Two of the three top-level workflows run automatically on a nightly `schedule`:

- **`windows-rtx-build-test.yml`** — full RTX source build + test, nightly.
  Also accepts `workflow_dispatch`, where subset inputs can narrow the matrix.
- **`windows-woa-build-test.yml`** — WoA (arm64) source build + test, nightly.
  Scheduled only; a manual trigger is deliberately not offered.
- **`windows-rtx-wheel-test.yml`** — published-wheel test. Its nightly cron is
  currently commented out, so it runs on `workflow_dispatch` only.

The reusable workflows (`_rtx-build.yml`, `_rtx-test.yml`, `_woa-build.yml`,
`_woa-test.yml`) are called by the orchestrators and are not run directly.

Detailed reference — workflow table, job naming, install paths, default matrix,
test environment variables, and runner diagnostics — is documented in
[docs/ci-details.md](docs/ci-details.md). The Windows-on-Arm reference lives in
[docs/woa-ci.md](docs/woa-ci.md).

# Cross-Repository CI Relay (CRCR)

This repository implements the downstream side of
[RFC-0050](https://github.com/pytorch/rfcs/blob/master/RFC-0050-Cross-Repository-CI-Relay-for-PyTorch-Out-of-Tree-Backends.md):

1. PyTorch sends a `repository_dispatch` event named `pytorch-pr-trigger` with
   the upstream PR metadata.
2. `windows-rtx-build-test.yml` resolves the upstream PR head, builds PyTorch on
   NVIDIA's Windows RTX infrastructure, and runs the configured test matrix.
3. Runs use PR-based concurrency so a newer update supersedes stale work for the
   same upstream PR.
4. The dispatch path is currently parked behind a `dispatch-gate` guard: the
   event is acknowledged and its payload logged, but the build and test fanout
   stays dormant until the upstream relay actions are published.

The exact event-to-workflow mapping and current relay status are documented in
the [CRCR section of the CI reference](docs/ci-details.md#rfc-0050-mapping).

# Performance

Not applicable — this repository provides CI infrastructure rather than a
shippable runtime artifact.

## Releases & Roadmap

This repo is CI infrastructure and does not publish versioned releases. Changes
land via pull request to `main`, while pre-release wheels are published through
the NVIDIA nightly index.

# Contribution Guidelines

Refer to [CONTRIBUTING.md](CONTRIBUTING.md).

## Governance & Maintainers

Maintained by the NVIDIA PyTorch Windows CI team. See the
[CUDA - Windows on Arm support policy](docs/support.md) for the responsible
maintainers, issue-routing guidance, escalation contact, and response target.

## Security

Please report security vulnerabilities responsibly. See
[SECURITY.md](SECURITY.md) for the disclosure process. Do not file public issues
for security reports. Vulnerabilities affecting in-tree PyTorch code also follow
the upstream [PyTorch security policy](https://github.com/pytorch/pytorch/security/policy).

## Support

PyTorch bugs should be reported upstream, while CI, build, packaging, workflow,
and published-wheel infrastructure issues should be reported in this
repository. See the [support policy](docs/support.md) for routing details and
the one-week acknowledgement target.

# Community

Discussion happens through GitHub issues and pull requests on this repository.

# References

- [RFC-0050: Cross-Repository CI Relay for PyTorch Out-of-Tree Backends](https://github.com/pytorch/rfcs/blob/master/RFC-0050-Cross-Repository-CI-Relay-for-PyTorch-Out-of-Tree-Backends.md)
- [pytorch/pytorch](https://github.com/pytorch/pytorch)
- [Detailed CI architecture and reference](docs/ci-details.md)
- [Windows-on-Arm (WoA) CI guide](docs/woa-ci.md)
- [Windows x86_64 build guide](docs/build_pytorch_windows_x86_64.md)
- [Windows ARM64 build guide](docs/build_pytorch_windows_arm64.md)

# License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for the
full text and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for third-party
OSS notices.
