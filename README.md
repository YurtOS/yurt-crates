# YurtOS crate registry

This repository contains the generator and static files for the YurtOS Cargo
crate registry. Published versions use the form `X.Y.Z+yurt.N`, where
`X.Y.Z` is the upstream crates.io version and `N` is the Yurt port revision.

Each publication creates a complete sparse-index snapshot under `index/<S>/`.
Published snapshots and crate archives are immutable; `latest` identifies the
current snapshot. The index generator reads Cargo-produced `.crate` archives
so its metadata and checksum describe the exact bytes Cargo downloads.
