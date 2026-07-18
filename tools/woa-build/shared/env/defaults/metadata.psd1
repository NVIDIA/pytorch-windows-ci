#
# Build-metadata overlay file. Merged into build-metadata.json by RepoBuildMetadata.ps1 when
# present. Site-specific path, default is the runner-side preset.
#

@{
    Domain   = 'Metadata'
    Defaults = @{
        ToolchainMetadataFile = 'C:\ci\woa\toolchain-metadata.json'
    }
}
