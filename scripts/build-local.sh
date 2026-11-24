#!/usr/bin/env bash
set -euo pipefail
umask 077

if [[ $# -lt 1 || $# -gt 2 || ! -f "$1" ]]; then
    echo "Usage: bash scripts/build-local.sh BASE.qcow2 [LAB-RUNNER.tar]" >&2
    exit 2
fi
base=$(realpath -- "$1")
archive=
if [[ $# == 2 ]]; then
    archive=$(realpath -e -- "$2")
fi
repo=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo"

# Fedora CoreOS stable 44.20261023.3.1, x86_64 QEMU disk (uncompressed).
base_sha=d7f0a53e861968de47595d28a6e1b98b3cd288a6dd9bb9fe7f0c16ffa3b458f9
butane=${BUTANE:-build/tools/butane}
mkdir -p build
out=$(mktemp -d build/machine.XXXXXXXX)
cleanup_partial_build() {
    rm -f -- "$out/machine.ign" "$out/machine.qcow2" "$out/SHA256SUMS" "$out/runner.iso" "$out/base.ign"
    rmdir -- "$out"
}
trap cleanup_partial_build EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$butane" --strict --pretty machine/sovereign-linux.bu > "$out/machine.ign"
if [[ -n "$archive" ]]; then
    expected=4f4219297165cbd9e47d2219c1a4aa5196b9257ef99e5eb693fc7818967e34f0
    echo "$expected  $archive" | sha256sum --check >&2
    mv "$out/machine.ign" "$out/base.ign"
    jq --rawfile unit runner/var-mnt-runner.mount \
        '.systemd.units += [{name:"var-mnt-runner.mount",enabled:true,contents:$unit}]' \
        "$out/base.ign" > "$out/machine.ign"
    rm -- "$out/base.ign"
    xorriso -as mkisofs -quiet -r -V SOVRUNNER -o "$out/runner.iso" -graft-points \
        "runner.tar=$archive" "start.sh=runner/start.sh"
fi
cp --reflink=auto --sparse=always -- "$base" "$out/machine.qcow2"
(
    cd "$out"
    echo "$base_sha  machine.qcow2" | sha256sum --check >&2
    qemu-img check -f qcow2 machine.qcow2 >&2
    sha256sum machine.ign machine.qcow2 > SHA256SUMS
    if [[ -f runner.iso ]]; then
        sha256sum runner.iso >> SHA256SUMS
    fi
)
trap - EXIT INT TERM
printf '%s\n' "$repo/$out"
