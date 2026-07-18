#
# Extension defaults — torchaudio / torchvision build flow, vcpkg layout, codec toggles, and the
# parent dir for the per-job work tree.
#

@{
    Domain   = 'Extensions'
    Defaults = @{
        TorchaudioGitUrl             = 'https://github.com/pytorch/audio.git'
        TorchvisionGitUrl            = 'https://github.com/pytorch/vision.git'
        TorchvisionDelvewheelExclude = 'torch_cuda.dll;c10_cuda.dll;torch_cpu.dll;c10.dll'

        # Per-job extension work tree on C: job scratch (single-drive runners; wiped by
        # woa-strict-clean), matching WOA_SCRATCH / WheelOutDir (build-meta.psd1).
        ExtensionWinWorkParent       = 'C:\ci\woa\scratch\ext-work'
        # vcpkg installed tree from the infra provisioner's vcpkg root (VcpkgRoot = C:\DevToolKit\vcpkg).
        TorchvisionWinVcpkgInstalled = 'C:\DevToolKit\vcpkg\installed\arm64-windows'

        TorchvisionUsePng    = '1'
        TorchvisionUseJpeg   = '1'
        TorchvisionUseWebp   = '1'
        TorchvisionUseNvjpeg = '1'
    }
}
