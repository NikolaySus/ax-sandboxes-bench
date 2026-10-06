# Substrate compatibility patch

`substrate-overlayfs-legacy.patch` applies to Substrate commit
`672533541dbfcd29084e4de2475267088bda3651`.

This host runs Linux 6.5. Upstream uses `fsconfig(..., "lowerdir+", ...)`,
which requires Linux 6.8 according to the
[Linux overlayfs documentation](https://cdn.kernel.org/doc/html/latest/filesystems/overlayfs.html).
The patch falls back to the legacy overlay mount API only if the first
`lowerdir+` option returns EINVAL. It rejects unencodable paths and option
strings exceeding a kernel page. It does not change the sandbox runtime,
workload image, snapshot policy, or resource limits.

Validation: upstream `go test ./internal/imagecache` plus real sandbox
creation and lifecycle smoke tests on the target kernel. The patch hash is
included in benchmark metadata. All worker images used for results must
be built with this patch; modern kernels continue using the original API.
