#!/usr/bin/env bash
# Point this checkout at the committed hooks in .githooks (SB-1421).
# Run once per clone, on every machine: git does not copy hook settings.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
chmod +x .githooks/*
git config core.hooksPath .githooks
echo "hooks installed: core.hooksPath = $(git config core.hooksPath)"
if [[ -f "${TRD_PRIVATE_TERMS:-$HOME/.config/trd/private-terms.txt}" ]]; then
    echo "private terms: present"
else
    echo "private terms: none — add one term per line to ~/.config/trd/private-terms.txt (docs/privacy.md)"
fi
