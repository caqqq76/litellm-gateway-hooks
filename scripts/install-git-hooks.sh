#!/usr/bin/env bash
# Turn on the repo's git hooks (.githooks/), which block commits and pushes that contain secrets.
# Downloads a pinned, checksum-verified gitleaks into .git/tools/. Run once per clone.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
. .githooks/common.sh

case "$(uname -s)_$(uname -m)" in
  Linux_x86_64)              PLATFORM=linux_x64    SHA256=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb ;;
  Linux_aarch64|Linux_arm64) PLATFORM=linux_arm64  SHA256=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080 ;;
  Darwin_x86_64)             PLATFORM=darwin_x64   SHA256=dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709 ;;
  Darwin_arm64)              PLATFORM=darwin_arm64 SHA256=b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5 ;;
  *) PLATFORM= ;;
esac

if [ -x "$GITLEAKS_DIR/gitleaks" ]; then
  echo "gitleaks $GITLEAKS_VERSION already installed in $GITLEAKS_DIR"
elif [ -n "$PLATFORM" ]; then
  tarball="gitleaks_${GITLEAKS_VERSION}_${PLATFORM}.tar.gz"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL --retry 3 -o "$tmp/$tarball" \
    "https://github.com/gitleaks/gitleaks/releases/download/v$GITLEAKS_VERSION/$tarball"
  if command -v sha256sum >/dev/null 2>&1; then
    actual="$(sha256sum "$tmp/$tarball" | cut -d' ' -f1)"
  else
    actual="$(shasum -a 256 "$tmp/$tarball" | cut -d' ' -f1)"
  fi
  if [ "$actual" != "$SHA256" ]; then
    echo "error: checksum mismatch for $tarball (got $actual)" >&2
    exit 1
  fi
  tar xzf "$tmp/$tarball" -C "$tmp" gitleaks
  mkdir -p "$GITLEAKS_DIR"
  mv "$tmp/gitleaks" "$GITLEAKS_DIR/gitleaks"
  echo "installed gitleaks $GITLEAKS_VERSION in $GITLEAKS_DIR"
elif command -v gitleaks >/dev/null 2>&1; then
  echo "no pinned gitleaks for $(uname -s) $(uname -m); the hooks will use $(command -v gitleaks)"
else
  echo "error: no pinned gitleaks for $(uname -s) $(uname -m). Install gitleaks $GITLEAKS_VERSION on PATH and rerun." >&2
  exit 1
fi

git config core.hooksPath .githooks
echo "git hooks enabled: pre-commit and pre-push scan for secrets with gitleaks"
