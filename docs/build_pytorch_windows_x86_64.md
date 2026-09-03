<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: MIT
-->

# Building PyTorch on Windows x86_64 with CUDA

This guide builds PyTorch from source on x86_64 Windows using Visual Studio
2022, x86_64 Python, CUDA, cuDNN, and MKL.

## 1. Install dependencies

- Visual Studio 2022 Build Tools with the C++ workload, Windows SDK, and x64 tools.
- x86_64 Python supported by the PyTorch revision being built.
- CUDA Toolkit 12.8, 13.0, or 13.2.
- cuDNN matching the selected CUDA Toolkit.
- CMake, Ninja, and Git with long paths enabled.

The examples use CUDA 13.0 and build for `8.9;12.0`.

## 2. Prepare the source and Python environment

```powershell
git clone --recursive https://github.com/pytorch/pytorch.git C:\src\pytorch
Set-Location C:\src\pytorch
git config --global core.longpaths true

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m pip install mkl==2024.2.0 mkl-static==2024.2.0 mkl-include==2024.2.0 ninja
```

After changing the PyTorch branch or tag, update its submodules:

```powershell
git submodule update --init --recursive
```

## 3. Activate Visual Studio 2022

From Command Prompt, activation changes the current `cmd.exe` environment:

```bat
call "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat" x64
```

The `call` keyword is required inside another batch script so control returns to
the caller.

From PowerShell, use Visual Studio's PowerShell entrypoint:

```powershell
& 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\Launch-VsDevShell.ps1' `
    -Arch amd64 -HostArch amd64 -SkipAutomaticLocation
```

Running `cmd /c vcvarsall.bat x64` alone does not activate the parent PowerShell
session: the environment disappears when the child `cmd.exe` exits. Run the
build in the same shell that was activated.

Verify the compiler:

```powershell
where.exe cl
cl 2>&1 | Select-String 'x64'
```

## 4. Set the build environment

```powershell
$env:USE_CUDA = '1'
$env:USE_CUDNN = '1'
$env:USE_XPU = '0'
$env:BUILD_TEST = '1'
$env:DISTUTILS_USE_SDK = '1'
$env:CMAKE_GENERATOR = 'Ninja'

Remove-Item Env:CMAKE_CUDA_ARCHITECTURES -ErrorAction SilentlyContinue
Remove-Item Env:CUDAARCHS -ErrorAction SilentlyContinue

$env:CUDA_VERSION = '13.0'
$cuda = "C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v$env:CUDA_VERSION"
$env:CUDA_PATH = $cuda
$env:CUDA_HOME = $cuda
$env:CUDA_TOOLKIT_ROOT_DIR = $cuda
$env:CUDNN_ROOT_DIR = $cuda
$env:CUDNN_LIB_DIR = "$cuda\lib\x64"
$env:PATH = "$cuda\bin;$env:PATH"

$env:BLAS = 'MKL'
$env:USE_MKL = '1'
$env:CMAKE_INCLUDE_PATH = "$env:VIRTUAL_ENV\Library\include"
$env:CMAKE_LIBRARY_PATH = "$env:VIRTUAL_ENV\Library\lib"
$env:TORCH_CUDA_ARCH_LIST = '8.9;12.0'
```

PyTorch uses `TORCH_CUDA_ARCH_LIST`, not `CMAKE_CUDA_ARCHITECTURES`. If the
latter remains in an existing cache, delete `build` before rebuilding.

To reduce CPU or memory usage, optionally set:

```powershell
$env:MAX_JOBS = '4'
```

## 5. Build and verify

For a clean build after changing toolchains or feature flags:

```powershell
Remove-Item build -Recurse -Force -ErrorAction SilentlyContinue
```

Install PyTorch as editable:

```powershell
python -m pip install -e . -v --no-build-isolation
```

Verify CUDA:

```powershell
python -c @"
import torch
print('version:  ', torch.__version__)
print('cuda:     ', torch.version.cuda)
print('available:', torch.cuda.is_available())
if torch.cuda.is_available():
    print('device:   ', torch.cuda.get_device_name(0))
    x = torch.randn(4, 4, device='cuda')
    print('smoke ok: ', (x @ x).sum().item())
"@
```

Expected result: `torch.cuda.is_available()` is `True` and the matrix operation
prints a number.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `cl.exe` targets the wrong machine | Wrong Visual Studio target environment | Reopen the shell and activate `vcvarsall.bat x64`. |
| `fatbinary fatal` with sccache | nvcc launcher incompatibility | Do not set `CMAKE_CUDA_COMPILER_LAUNCHER`. |
| Feature flag is ignored | Stale CMake cache | Delete `build` and rebuild. |
