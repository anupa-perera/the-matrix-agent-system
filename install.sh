#!/usr/bin/env sh
set -eu

DEFAULT_SOURCE="https://github.com/anupa-perera/the-matrix-agent-system/archive/refs/heads/main.zip"
SOURCE="${MATRIX_SOURCE:-}"
PYTHON_VERSION="${MATRIX_PYTHON:-3.13}"
SKIP_START="${MATRIX_SKIP_START:-0}"
NO_FORCE="${MATRIX_NO_FORCE:-0}"

write_step() {
    printf "\n==> %s\n" "$1" >&2
}

script_dir() {
    case "$0" in
        */*)
            dirname "$0"
            ;;
        *)
            pwd
            ;;
    esac
}

resolve_source() {
    if [ -n "$SOURCE" ]; then
        printf "%s" "$SOURCE"
        return
    fi

    dir="$(script_dir)"
    if [ -f "$dir/pyproject.toml" ]; then
        cd "$dir" && pwd
        return
    fi

    printf "%s" "$DEFAULT_SOURCE"
}

find_uv() {
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi

    for candidate in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
        if [ -x "$candidate" ]; then
            printf "%s\n" "$candidate"
            return 0
        fi
    done

    return 1
}

install_uv_if_missing() {
    if uv_path="$(find_uv)"; then
        printf "uv found: %s\n" "$uv_path" >&2
        printf "%s\n" "$uv_path"
        return
    fi

    write_step "Installing uv"
    printf "This downloads uv from the official Astral installer.\n" >&2

    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        printf "curl or wget is required to install uv.\n" >&2
        exit 1
    fi

    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if uv_path="$(find_uv)"; then
        printf "%s\n" "$uv_path"
        return
    fi

    printf "uv was installed, but uv was not found on PATH. Close and reopen your terminal, then run this installer again.\n" >&2
    exit 1
}

find_matrix() {
    if command -v the-matrix >/dev/null 2>&1; then
        command -v the-matrix
        return 0
    fi

    candidate="$HOME/.local/bin/the-matrix"
    if [ -x "$candidate" ]; then
        printf "%s\n" "$candidate"
        return 0
    fi

    return 1
}

printf "The Matrix installer\n"
printf "This installs the CLI for the current user. Administrator rights are not required.\n"

SOURCE="$(resolve_source)"
UV_PATH="$(install_uv_if_missing)"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
export UV_SYSTEM_CERTS=true

write_step "Installing The Matrix Agent System"
printf "Using uv: %s\n" "$UV_PATH"
printf "Source: %s\n" "$SOURCE"

if [ "$NO_FORCE" = "1" ]; then
    "$UV_PATH" tool install --python "$PYTHON_VERSION" "$SOURCE" || {
        printf "The Matrix installation failed. Close any open Matrix terminals, check the error above, and run the installer again.\n" >&2
        exit 1
    }
else
    "$UV_PATH" tool install --python "$PYTHON_VERSION" --force "$SOURCE" || {
        printf "The Matrix installation failed. Close any open Matrix terminals, check the error above, and run the installer again.\n" >&2
        exit 1
    }
fi

if ! MATRIX_COMMAND="$(find_matrix)"; then
    printf "The Matrix was installed, but the-matrix was not found. Close and reopen your terminal, then run: the-matrix start\n" >&2
    exit 1
fi

write_step "Installation complete"
printf "Command: %s\n" "$MATRIX_COMMAND"

if [ "$SKIP_START" = "1" ]; then
    printf "Run this when ready:\n"
    printf "  the-matrix start\n"
else
    write_step "Starting guided setup"
    "$MATRIX_COMMAND" start
fi
