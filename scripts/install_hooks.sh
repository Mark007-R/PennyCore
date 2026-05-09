#!/usr/bin/env bash
# Wire up the project's git hooks. One-time per clone.
#
#   bash scripts/install_hooks.sh
#
# Sets `core.hooksPath = .githooks` so git uses the in-repo hooks. This
# keeps the hooks under version control AND survives `git clone` (the
# default `.git/hooks` directory is local-only).
#
# After running this you can verify with:
#   git config --get core.hooksPath   # should print: .githooks

set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "${repo_root}"

git config core.hooksPath .githooks
chmod +x .githooks/pre-commit 2>/dev/null || true   # no-op on Windows/NTFS

echo "✓ git hooks wired: $(git config --get core.hooksPath)"
echo "  pre-commit installed: enforces takehome evaluate.py checksum freeze,"
echo "  refuses local-only diary/SKILL files, scans for secret patterns."
