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

This repository hosts the GitHub Actions workflows that build and test PyTorch
on NVIDIA's self-hosted Windows + RTX runner pool. It implements the downstream
half of [RFC-0050: Cross-Repository CI Relay for PyTorch Out-of-Tree
Backends](https://github.com/pytorch/rfcs/blob/master/RFC-0050-Cross-Repository-CI-Relay-for-PyTorch-Out-of-Tree-Backends.md).

Upstream covers a single configuration (Python 3.12, CUDA 12.8); this repo
deliberately expands the matrix to catch regressions across multiple Python and
CUDA toolkit combinations before they show up upstream. The build/test logic
comes entirely from PyTorch's in-tree `.ci/pytorch/*.sh` scripts — this repo
holds only the workflow wiring. Every job runs on a self-hosted NVIDIA runner;
there are no GitHub-hosted (cloud) runs.

> **Full architecture, matrix, and runner model:**
> [docs/ci-details.md](docs/ci-details.md).

> **Windows-on-Arm (WoA) CI:** see [docs/woa-ci.md](docs/woa-ci.md) for the arm64
> build/test matrix, runner contract, and operator guide.

# Getting Started

The two RTX top-level workflows run automatically on a nightly schedule. No action
is required to run them:

- **`windows-rtx-build-test.yml`** — full source build + test (nightly).
- **`windows-rtx-wheel-test.yml`** — nightly published-wheel test.

Schedules are documented in [docs/ci-details.md](docs/ci-details.md).

The Windows-on-Arm workflow runs on a nightly schedule:

- **`windows-woa-build-test.yml`** — WoA (arm64) source build + test (nightly
  `schedule`; no manual trigger). See [docs/woa-ci.md](docs/woa-ci.md).

# Requirements

- Self-hosted Windows runners from NVIDIA infrastructure, labelled for the
  build/test pools (`rtx-build`, `rtx-40x0-test`, `rtx-50x0-test`) and tagged per
  Python/CUDA cell (`py3xx`, `cu1xx`).
- Ephemeral, pre-prepped runner images carrying Python, the CUDA toolkit +
  driver, MSVC build tools, and the PyTorch test runtime — the workflows do zero
  in-job setup. See [docs/ci-details.md](docs/ci-details.md) for the full image
  contents and label routing.
- Windows-on-Arm (arm64) runners share a single persistent pool labelled
  `woa-arm64` for both build and test, with the toolchain preinstalled and a clean
  per-job venv built in-job. See [docs/woa-ci.md](docs/woa-ci.md) for the runner
  contract.

# Usage

The nightly schedules run both RTX top-level workflows automatically. The reusable
workflows (`_rtx-build.yml`, `_rtx-test.yml`) are called by the two orchestrators
and are not run directly.

Detailed reference — workflow table, job naming, install paths, default matrix,
test environment variables, and runner diagnostics — is documented in
[docs/ci-details.md](docs/ci-details.md).

The Windows-on-Arm orchestrator (`windows-woa-build-test.yml`) similarly calls the
reusable `_woa-build.yml` / `_woa-test.yml` workflows, which are not run directly.
Its reference lives in [docs/woa-ci.md](docs/woa-ci.md).

# Performance

Not applicable — this repository provides CI infrastructure rather than a
shippable runtime artifact.

## Releases & Roadmap

This repo is CI infrastructure and does not publish versioned releases. Changes
land via pull request to `main`.

# Contribution Guidelines

Refer to [CONTRIBUTING.md](CONTRIBUTING.md).


## Governance & Maintainers

Maintained by the NVIDIA PyTorch Windows CI team. Open an issue or pull request
for questions, triage, or proposed changes.

## Security

Please report security vulnerabilities responsibly. See [SECURITY.md](SECURITY.md)
for the disclosure process. Do not file public issues for security reports.

## Support

Maintained on a best-effort basis. For questions, bugs, or feature requests,
please open a GitHub issue in this repository.

# Community

Discussion happens through GitHub issues and pull requests on this repository.

# References

- [RFC-0050: Cross-Repository CI Relay for PyTorch Out-of-Tree Backends](https://github.com/pytorch/rfcs/blob/master/RFC-0050-Cross-Repository-CI-Relay-for-PyTorch-Out-of-Tree-Backends.md)
- [pytorch/pytorch](https://github.com/pytorch/pytorch)
- [Detailed CI architecture & reference](docs/ci-details.md)
- [Windows-on-Arm (WoA) CI guide](docs/woa-ci.md)

# License

This project is licensed under the MIT License — see [LICENSE](LICENSE) for the
full text and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for third-party
OSS notices.
