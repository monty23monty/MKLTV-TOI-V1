#!/bin/sh
# Load a published, checksummed Linux amd64 release without a registry login.
# This imports images only. It never updates a running stack or its database.
set -eu

release_tag=${1:-}
if ! printf '%s\n' "$release_tag" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$'; then
    printf 'Usage: sh scripts/load-release-images.sh v2.0.1\n' >&2
    exit 2
fi
case "$(uname -m)" in
    x86_64|amd64) ;;
    *) printf 'This release bundle supports Linux amd64 hosts only.\n' >&2; exit 2 ;;
esac
command -v docker >/dev/null
command -v curl >/dev/null
command -v sha256sum >/dev/null
docker info >/dev/null
release_dir=$(mktemp -d)
bundle="mkltoi-${release_tag}-linux-amd64.tar.gz"
trap 'rm -f "$release_dir/SHA256SUMS" "$release_dir/$bundle"; rmdir "$release_dir"' EXIT HUP INT TERM
release_url="https://github.com/monty23monty/MKLTV-TOI-V1/releases/download/${release_tag}"
curl --fail --location --show-error --retry 3 "$release_url/SHA256SUMS" -o "$release_dir/SHA256SUMS"
curl --fail --location --show-error --retry 3 "$release_url/$bundle" -o "$release_dir/$bundle"
(
    cd "$release_dir"
    sha256sum --check --ignore-missing SHA256SUMS
)
docker image load --input "$release_dir/$bundle"
for component in backend frontend; do
    image="ghcr.io/monty23monty/mkltv-toi-${component}:${release_tag}"
    actual_version=$(docker image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.version"}}')
    if [ "$actual_version" != "${release_tag#v}" ]; then
        printf 'Unexpected version on imported image %s\n' "$image" >&2
        exit 1
    fi
done
printf 'Both %s images are loaded. Update the existing stack with TOI_RELEASE_TAG=%s.\n' "$release_tag" "$release_tag"
