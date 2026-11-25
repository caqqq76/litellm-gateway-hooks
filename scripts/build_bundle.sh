#!/usr/bin/env bash
# Build the offline bundle: build/litellm-gateway-hooks-<version>-bundle.tar.gz (needs network, run on a build host).
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION="$(sed -n 's/^version = "\(.*\)"/\1/p' pyproject.toml)"
NAME="litellm-gateway-hooks-$VERSION-bundle"
B="build/$NAME"

rm -rf dist "$B" "build/$NAME.tar.gz"
uv build -q
mkdir -p "$B/wheelhouse" "$B/examples" "$B/app_example"
uvx pip download --quiet --only-binary=:all: --no-deps --python-version 3.10 -d "$B/wheelhouse" redis==8.1.0 async-timeout==5.0.1 tzdata==2026.6
cp dist/* "$B/wheelhouse/"

sed "s/@VERSION@/$VERSION/g" scripts/install.sh.in > "$B/install.sh"
chmod +x "$B/install.sh"
cp gateway-hooks.yaml "$B/examples/gateway-hooks.yaml"
cp scripts/proxy_config.example.yaml "$B/examples/proxy_config.yaml"
cp README.md APP_ENGINEER_GUIDE.md "$B/"
cp app_example/llm_client.py "$B/app_example/"
(cd "$B" && find . -type f ! -name SHA256SUMS | sort | xargs sha256sum > SHA256SUMS)
tar czf "build/$NAME.tar.gz" -C build "$NAME"
sha256sum "build/$NAME.tar.gz"
