# CUDA - Windows on Arm platform support

This document describes how CUDA - Windows on Arm integrates with upstream
PyTorch and defines the scope of the stable and nightly packages published by
NVIDIA. A feature is considered supported only when it is included in the
published package and exercised by repeatable CI. Features that are compiled
but not exercised by CI are identified as untested.

## PyTorch integration

CUDA - Windows on Arm wheels are built from a pinned commit of the upstream
[`pytorch/pytorch`](https://github.com/pytorch/pytorch) repository. The nightly
workflow follows upstream PyTorch development and records the exact PyTorch,
TorchVision, and TorchAudio source revisions used for each build.

The platform-specific build, packaging, test orchestration, and PyTorch HUD
reporting are maintained in this repository:

- [Windows-on-Arm nightly workflow](../.github/workflows/windows-woa-build-test.yml)
- [Windows-on-Arm build implementation](../tools/woa-build)
- [Windows-on-Arm CI and runner contract](woa-ci.md)
- [Windows ARM64 source-build guide](build_pytorch_windows_arm64.md)
- [PyTorch CRCR results](https://hud.pytorch.org/crcr/NVIDIA/pytorch-windows-ci)

Upstream PyTorch also provides a
[Windows ARM64 wheel workflow](https://github.com/pytorch/pytorch/blob/main/.github/workflows/generated-windows-arm64-binary-wheel-nightly.yml)
and a
[Windows ARM64 build guide](https://github.com/pytorch/pytorch/wiki/Build-PyTorch-and-LibTorch-on-Windows-ARM64).

## Feature support

The table distinguishes functionality that is validated today from functionality
that is only built, imported, or under development.

| Advertised capability | Stable packages | Nightly packages | CI coverage and evidence |
| --- | --- | --- | --- |
| Install and import PyTorch | Supported | Supported | `_woa-test.yml` installs the wheel into a clean environment and requires `import torch` to succeed. |
| CUDA discovery | Supported | Supported | The same smoke test requires `torch.cuda.is_available()` to be true. |
| CUDA tensors and operators | Supported | Supported | The nightly test runner invokes upstream `test/run_test.py` with `test_cuda`; the result contributes to the authoritative CRCR test conclusion. |
| PyTorch autograd and `torch.nn` on CUDA | Supported where exercised by `test_cuda` | Supported where exercised by `test_cuda` | Coverage is inherited from the executed upstream CUDA tests. APIs outside that selection are not implied to be covered. |
| TorchVision | Package available; functional coverage is limited | Package available; functional coverage is limited | CI builds the extension and smoke-tests `import torchvision`; a functional operator suite is not run. |
| TorchAudio | Package available; functional coverage is limited | Package available; functional coverage is limited | CI builds the extension and smoke-tests `import torchaudio`; a functional operator suite is not run. |

Package presence alone does not establish full API support. Consult the
[Windows-on-Arm workflow](../.github/workflows/windows-woa-build-test.yml) and
the linked CRCR results for the configuration and tests exercised by a specific
nightly run.

## Disabled and untested functionality

### Disabled

- oneDNN/MKLDNN and its Arm Compute Library integration are disabled while the
  current Windows ARM64 compiler cannot build the required oneDNN sources.
- MAGMA is disabled.
- x64 Python and x64-emulated Python source-build environments are not supported.

### Built but not validated by the current nightly suite

- Distributed support is compiled in, but distributed tests are excluded.
- Quantization tests are excluded.
- JIT executor tests are excluded.
- `torch.compile` and Inductor are not claimed as supported without a dedicated
  passing test job.
- Multi-GPU behavior is not claimed without a dedicated passing test job.
- Full TorchVision and TorchAudio functional and operator coverage is not run.
- Python versions not enabled in the current Windows-on-Arm workflow matrix are
  not covered by nightly CI.

The current test selection is defined by the
[Windows-on-Arm test runner](../tools/woa-build/shared/test/TestShardRunner.ps1).
Compiling a feature does not by itself make that feature supported.

## CI evidence

Nightly Windows-on-Arm results are reported as two stable logical jobs:
`win-woa-arm64-cu134 / build` and `win-woa-arm64-cu134 / test (arm64)`.
The test conclusion includes all expected Python cells and test shards; missing
reports or a failing shard make the reported test job fail.

- [PyTorch CRCR nightly results for the last seven days](https://hud.pytorch.org/crcr/NVIDIA/pytorch-windows-ci?event=nightly&days=7)
- [Windows-on-Arm workflow runs](https://github.com/NVIDIA/pytorch-windows-ci/actions/workflows/windows-woa-build-test.yml)
- [Representative successful Windows-on-Arm run](https://github.com/NVIDIA/pytorch-windows-ci/actions/runs/35081946521)
- [CRCR reporting design and failure rules](crcr-hud-reporting.md)
