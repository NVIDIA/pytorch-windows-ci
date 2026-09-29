<!--
SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
SPDX-License-Identifier: MIT
-->

# Building PyTorch on Windows ARM64 with CUDA

This guide builds and repacks a CUDA-enabled PyTorch wheel on native Windows
ARM64 using Visual Studio 2026, ARM64 Python, CUDA 13.4, cuDNN, APL, and libuv.

## 1. Install dependencies

- Visual Studio 2026 Build Tools with the C++ workload, Windows SDK, and ARM64 tools.
- Native ARM64 Python supported by the PyTorch revision being built; do not use
  x64-emulated Python.
- CUDA Toolkit 13.4 at `C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.4`.
- cuDNN 9.26 at `C:\Program Files\NVIDIA\CUDNN\v9.26`.
- Arm Performance Libraries, for example `C:\DevToolKit\APL\armpl_26.01`.
- vcpkg libuv at `C:\DevToolKit\vcpkg\packages\libuv_arm64-windows`.
- CMake, Ninja, and Git.

Install libuv when needed:

```powershell
git clone https://github.com/microsoft/vcpkg.git C:\DevToolKit\vcpkg
C:\DevToolKit\vcpkg\bootstrap-vcpkg.bat
C:\DevToolKit\vcpkg\vcpkg install libuv:arm64-windows
```

## 2. Prepare the source and Python environment

```powershell
git clone --recursive https://github.com/pytorch/pytorch.git C:\src\pytorch
Set-Location C:\src\pytorch
git config --global core.longpaths true

# Reject x64 or x86 Python before creating .venv; otherwise the build can link
# torch_python against an import library for the wrong architecture.
$python = (Get-Command python -ErrorAction Stop).Source
$stream = [System.IO.File]::OpenRead($python)
$reader = [System.IO.BinaryReader]::new($stream)
try {
    $stream.Position = 0x3c
    $peOffset = $reader.ReadInt32()
    $stream.Position = $peOffset + 4
    $machine = $reader.ReadUInt16()
} finally {
    $reader.Dispose()
    $stream.Dispose()
}
if ($machine -ne 0xAA64) {
    throw "Native ARM64 Python is required. Provision an ARM64 interpreter and put it first on PATH. Found: $python"
}

& $python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

After changing the PyTorch branch or tag, update its submodules:

```powershell
git submodule update --init --recursive
```

## 3. Activate Visual Studio 2026

Visual Studio tools may use either of these roots, depending on whether the
Build Tools layout or the full Visual Studio layout is installed:

- `C:\Program Files (x86)\Microsoft Visual Studio\2026\BuildTools`
- `C:\Program Files\Microsoft Visual Studio\18\BuildTools`

From Command Prompt, call `vcvarsarm64.bat` from the root present on the machine.
Activation changes the current `cmd.exe` environment:

```bat
call "C:\Program Files (x86)\Microsoft Visual Studio\2026\BuildTools\VC\Auxiliary\Build\vcvarsarm64.bat"
```

or:

```bat
call "C:\Program Files\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsarm64.bat"
```

The `call` keyword is required inside another batch script so control returns to
the caller.

From PowerShell, select the installed tools directory and use Visual Studio's
PowerShell entrypoint:

```powershell
$vsTools = @(
    'C:\Program Files (x86)\Microsoft Visual Studio\2026\BuildTools\Common7\Tools'
    'C:\Program Files\Microsoft Visual Studio\18\BuildTools\Common7\Tools'
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1

if (-not $vsTools) {
    throw 'Visual Studio 2026 Build Tools were not found.'
}

& "$vsTools\Launch-VsDevShell.ps1" `
    -Arch arm64 -HostArch arm64 -SkipAutomaticLocation
```

Running `cmd /c vcvarsarm64.bat` alone does not activate the parent PowerShell
session: the environment disappears when the child `cmd.exe` exits. Run the
build in the same shell that was activated.

Verify the compiler:

```powershell
where.exe cl
cl 2>&1 | Select-String 'ARM64'
```

## 4. Set the ARM64 build environment

PyTorch's bundled `FindCUDA.cmake` uses x64-first library suffixes for any
64-bit Windows build. The complete override list below must remain until that
discovery behavior can be fixed. It also sets the internal Toolkit root values
so `FindCUDA.cmake` does not clear the ARM64 library overrides on first configure.

```powershell
$cuda = 'C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v13.4'
$cudnn = 'C:\Program Files\NVIDIA\CUDNN\v9.26'
$apl = 'C:\DevToolKit\APL\armpl_26.01'
$libuv = 'C:\DevToolKit\vcpkg\packages\libuv_arm64-windows'
$cupti = "$cuda\extras\CUPTI\lib\arm64"
$cudaRoot = $cuda.Replace('\', '/')
$cudaLib = "$cudaRoot/lib/arm64"
$cuptiLib = "$($cupti.Replace('\', '/'))/cupti.lib"

$cudaCMakeDefinitions = @(
    'SLEEF_DISABLE_SVE=ON'
    "CUDA_TOOLKIT_ROOT_DIR=$cudaRoot"
    "CUDA_TOOLKIT_ROOT_DIR_INTERNAL=$cudaRoot"
    "CUDA_TOOLKIT_TARGET_DIR=$cudaRoot"
    "CUDA_TOOLKIT_TARGET_DIR_INTERNAL=$cudaRoot"
    "CUDA_CUDART=$cudaLib/cudart.lib"
    "CUDA_CUDART_LIBRARY=$cudaLib/cudart.lib"
    "CUDA_CUDA_LIBRARY=$cudaLib/cuda.lib"
    "CUDA_OpenCL_LIBRARY=$cudaLib/OpenCL.lib"
    "CUDA_cublasLt_LIBRARY=$cudaLib/cublasLt.lib"
    "CUDA_cublas_LIBRARY=$cudaLib/cublas.lib"
    "CUDA_cuda_driver_LIBRARY=$cudaLib/cuda.lib"
    "CUDA_cudadevrt_LIBRARY=$cudaLib/cudadevrt.lib"
    "CUDA_cudart_LIBRARY=$cudaLib/cudart.lib"
    "CUDA_cudart_static_LIBRARY=$cudaLib/cudart_static.lib"
    "CUDA_cufft_LIBRARY=$cudaLib/cufft.lib"
    "CUDA_cufftw_LIBRARY=$cudaLib/cufftw.lib"
    "CUDA_cupti_LIBRARY=$cuptiLib"
    "CUDA_curand_LIBRARY=$cudaLib/curand.lib"
    "CUDA_cusolver_LIBRARY=$cudaLib/cusolver.lib"
    "CUDA_cusparse_LIBRARY=$cudaLib/cusparse.lib"
    "CUDA_nppc_LIBRARY=$cudaLib/nppc.lib"
    "CUDA_nppial_LIBRARY=$cudaLib/nppial.lib"
    "CUDA_nppicc_LIBRARY=$cudaLib/nppicc.lib"
    "CUDA_nppidei_LIBRARY=$cudaLib/nppidei.lib"
    "CUDA_nppif_LIBRARY=$cudaLib/nppif.lib"
    "CUDA_nppig_LIBRARY=$cudaLib/nppig.lib"
    "CUDA_nppim_LIBRARY=$cudaLib/nppim.lib"
    "CUDA_nppist_LIBRARY=$cudaLib/nppist.lib"
    "CUDA_nppisu_LIBRARY=$cudaLib/nppisu.lib"
    "CUDA_nppitc_LIBRARY=$cudaLib/nppitc.lib"
    "CUDA_npps_LIBRARY=$cudaLib/npps.lib"
    "CUDA_nvjpeg_LIBRARY=$cudaLib/nvjpeg.lib"
    "CUDA_nvml_LIBRARY=$cudaLib/nvml.lib"
    "CUDA_nvrtc_LIBRARY=$cudaLib/nvrtc.lib"
)

foreach ($definition in $cudaCMakeDefinitions) {
    $library = ($definition -split '=', 2)[1]
    if ($library -like '*.lib' -and -not (Test-Path -LiteralPath $library)) {
        throw "Required ARM64 CUDA library was not found: $library"
    }
}

$env:USE_CUDA = '1'
$env:USE_CUDNN = '1'
$env:USE_XPU = '0'
$env:BUILD_TEST = '1'
$env:DISTUTILS_USE_SDK = '1'
$env:CMAKE_GENERATOR = 'Ninja'
Remove-Item Env:CMAKE_CUDA_ARCHITECTURES -ErrorAction SilentlyContinue
Remove-Item Env:CUDAARCHS -ErrorAction SilentlyContinue

$env:CUDA_VERSION = '13.4'
$env:CUDA_PATH = $cuda
$env:CUDA_HOME = $cuda
$env:CUDAToolkit_ROOT = $cuda
$env:CUDA_TOOLKIT_ROOT_DIR = $cuda
$env:CMAKE_CUDA_COMPILER = "$cuda\bin\nvcc.exe"
$env:PATH = "$cuda\bin;$env:PATH"

$env:CUDNN_ROOT_DIR = $cudnn
$env:CUDNN_INCLUDE_DIR = "$cudnn\include\13.4"
$env:CUDNN_LIB_DIR = "$cudnn\lib\13.4\arm64"

$env:BLAS = 'APL'
$env:USE_LAPACK = '1'
$env:APL_INCLUDE_DIR = "$apl\include"
$env:APL_LIB_DIR = "$apl\lib"

$env:USE_DISTRIBUTED = '1'
$env:libuv_ROOT = $libuv
$env:CMAKE_PREFIX_PATH = "$libuv;$env:CMAKE_PREFIX_PATH"

$env:USE_MAGMA = '0'
$env:USE_MKLDNN = '0'
$env:USE_MKLDNN_ACL = '0'
$env:SKBUILD_CMAKE_DEFINE = $cudaCMakeDefinitions -join ';'
$env:TORCH_CUDA_ARCH_LIST = '8.9;10.3+PTX;12.0;12.1+PTX'
$env:CFLAGS = '/Zc:preprocessor /EHsc'
$env:CXXFLAGS = '/Zc:preprocessor /EHsc'
$env:CL = '/Zc:preprocessor /EHsc'
$env:CMAKE_CUDA_FLAGS = "$env:CMAKE_CUDA_FLAGS -Xcompiler /Zc:preprocessor"
```

To reduce CPU or memory usage, optionally set:

```powershell
$env:MAX_JOBS = '4'
```

## 5. Build the ARM64 wheel

Delete the existing cache after changing CUDA paths, compiler settings, or
library overrides:

```powershell
Remove-Item build -Recurse -Force -ErrorAction SilentlyContinue
```

Build the vanilla wheel:

```powershell
New-Item -ItemType Directory -Force -Path .\dist | Out-Null
python -m pip wheel . --no-deps --no-build-isolation -v -w .\dist
```

After configuration, confirm no required CUDA library points to x64:

```powershell
Select-String build\CMakeCache.txt -Pattern '^CUDA_.*=.+[/\\]x64[/\\]'
```

The command should produce no required `CUDA_*_LIBRARY` entries.

## 6. Repack the wheel with ARM64 runtime DLLs

The current build backend does not create the old
`build\lib.win-arm64-cpython-*\torch\lib` staging tree. Unpack the generated
wheel, add the ARM64 dependency DLLs to its `torch\lib`, and repack it so the
wheel `RECORD` is regenerated.

```powershell
$wheelOut = '.\dist'
$sourceWheel = Get-ChildItem -LiteralPath $wheelOut -Filter 'torch-*.whl' -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $sourceWheel) { throw "No torch wheel was found under $wheelOut" }

$unpackRoot = Join-Path $wheelOut 'unpacked'
$repackedOut = Join-Path $wheelOut 'repacked'
Remove-Item -LiteralPath $unpackRoot, $repackedOut -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $unpackRoot, $repackedOut | Out-Null

python -m wheel unpack $sourceWheel.FullName --dest $unpackRoot
if ($LASTEXITCODE -ne 0) { throw "Failed to unpack $($sourceWheel.FullName)" }

$unpackedWheel = Get-ChildItem -LiteralPath $unpackRoot -Directory |
    Select-Object -First 1
if (-not $unpackedWheel) { throw 'No unpacked wheel directory was created.' }

$torchLib = Join-Path $unpackedWheel.FullName 'torch\lib'
if (-not (Test-Path -LiteralPath $torchLib)) {
    throw 'The unpacked wheel does not contain torch\lib.'
}

$dllDirs = @(
    "$cuda\bin\arm64"
    "$cuda\extras\CUPTI\bin\arm64"
    "$cuda\extras\CUPTI\lib\arm64"
    "$cudnn\bin\13.4\arm64"
    "$apl\bin"
    "$libuv\bin"
) | Where-Object { Test-Path -LiteralPath $_ }

$dependencyDlls = $dllDirs | ForEach-Object {
    Get-ChildItem -LiteralPath $_ -Filter '*.dll' -File
}
if (-not $dependencyDlls) { throw 'No ARM64 dependency DLLs were found.' }

foreach ($dll in $dependencyDlls) {
    $headers = & dumpbin.exe /headers $dll.FullName 2>&1
    if ($LASTEXITCODE -ne 0 -or ($headers -join "`n") -notmatch 'AA64 machine \(ARM64\)') {
        throw "Dependency is not an ARM64 DLL: $($dll.FullName)"
    }
    Copy-Item -LiteralPath $dll.FullName -Destination $torchLib -Force
}

if (-not (Get-ChildItem -LiteralPath $torchLib -Filter 'cupti64_*.dll' -File -ErrorAction SilentlyContinue)) {
    throw 'No ARM64 cupti64_*.dll was added to the wheel.'
}

python -m wheel pack $unpackedWheel.FullName --dest-dir $repackedOut
if ($LASTEXITCODE -ne 0) { throw 'Failed to repack the ARM64 wheel.' }
```

Use the wheel under `.\dist\repacked`. Never copy a DLL from a CUDA `x64`
directory into the ARM64 wheel.

## 7. Install and verify

```powershell
python -m venv C:\venv\torch-cuda
C:\venv\torch-cuda\Scripts\Activate.ps1
$installWheels = @(Get-ChildItem -LiteralPath .\dist\repacked -Filter 'torch-*.whl' -File)
if ($installWheels.Count -ne 1) {
    throw "Expected exactly one repacked torch wheel; found $($installWheels.Count)."
}
python -m pip install $installWheels[0].FullName
python -m pip check
```

Run a CUDA smoke test without allowing the toolkit or cuDNN installation to
supply DLLs missing from the wheel:

```powershell
$savedPath = $env:PATH
$env:PATH = (($env:PATH -split ';') | Where-Object {
    $_ -and $_ -notlike "$cuda*" -and $_ -notlike "$cudnn*"
}) -join ';'
try {
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
} finally {
    $env:PATH = $savedPath
}
```

Expected result: `torch.cuda.is_available()` is `True` and the matrix operation
prints a number.

## Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `cl.exe` targets x64 | Wrong Visual Studio target environment | Reopen the shell and activate the ARM64 developer environment. |
| `warning LNK4272` or CUDA/Kineto unresolved symbols | An x64 import library was cached | Delete `build`, use the complete override list, and confirm no required cache entry points to `lib/x64`. |
| CMake cannot find cuDNN | Incorrect versioned cuDNN path | Use `include\13.4` and `lib\13.4\arm64`. |
| Import fails with a missing DLL | The DLL was omitted during repacking | Add its ARM64 directory to `$dllDirs` and repack. Never use an x64 DLL. |
| Feature flag is ignored | Stale CMake cache | Delete `build` and rebuild. |
