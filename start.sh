#!/usr/bin/env sh
set -eu

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

if ! command -v the-matrix >/dev/null 2>&1; then
    printf "The Matrix is not installed yet.\n" >&2
    printf "Run this first:\n" >&2
    printf "  sh install.sh\n" >&2
    exit 1
fi

exec the-matrix start "$@"
