# Shared by the git hooks in .githooks/ and scripts/install-git-hooks.sh. Not a hook itself.

GITLEAKS_VERSION=8.30.1
REPO_ROOT="$(git rev-parse --show-toplevel)"
GITLEAKS_DIR="$(git rev-parse --path-format=absolute --git-common-dir)/tools/gitleaks-$GITLEAKS_VERSION"

# Print the gitleaks to use: the pinned copy from install-git-hooks.sh, else one on PATH. Fail closed if none.
find_gitleaks() {
  if [ -x "$GITLEAKS_DIR/gitleaks" ]; then
    echo "$GITLEAKS_DIR/gitleaks"
  elif command -v gitleaks >/dev/null 2>&1; then
    command -v gitleaks
  else
    echo "blocked: gitleaks is not installed, so this cannot be checked for secrets." >&2
    echo "Run scripts/install-git-hooks.sh, then try again." >&2
    return 1
  fi
}

blocked() {
  cat >&2 <<EOF

blocked: gitleaks found a possible secret ($1). Nothing was $2.
- Remove the secret, and rotate it if it is real.
- If a commit already holds it, rewrite that commit (git commit --amend, or git rebase -i) before pushing.
- If it is a false positive, add a '# gitleaks:allow' comment on that line or an allowlist entry in .gitleaks.toml.
EOF
}
