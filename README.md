# YurtOS crate registry

This repository contains the generator and static files for the YurtOS Cargo
crate registry. Published versions use the form `X.Y.Z+yurt.N`, where
`X.Y.Z` is the upstream crates.io version and `N` is the Yurt port revision.

Each publication creates a complete sparse-index snapshot under `index/<S>/`.
Published snapshots and crate archives are immutable; `latest` identifies the
current snapshot. The index generator reads Cargo-produced `.crate` archives
so its metadata and checksum describe the exact bytes Cargo downloads.

## Use a fixed snapshot with Cargo

Choose a snapshot number and configure Cargo to use that sparse index. Pinning
the snapshot keeps the registry view stable; a newer fix is selected by moving
the URL to a later snapshot.

```sh
cargo --config 'registries.yurt.index="sparse+https://yurtos.github.io/yurt-crates/index/2/"' \
  --config 'patch.crates-io.rustix.package="rustix"' \
  --config 'patch.crates-io.rustix.version="=1.1.5+yurt.1"' \
  --config 'patch.crates-io.rustix.registry="yurt"' build
```

The selected package contains patched Rust source, and the published `.crate`
archives are public. The publishing workflow checks out private `yurt-ports`
at the requested commit, so its build and test output appears in public GitHub
Actions logs as well. Do not put credentials or other secrets in port build
output.

This is a static sparse registry. Cargo registry API commands such as
`cargo search` are not available; use the listing at
<https://yurtos.github.io/yurt-crates/> instead.
