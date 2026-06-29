#!/usr/bin/env sh
set -eu

REPO_ARCHIVE_URL="${AWSUTILS_ARCHIVE_URL:-https://github.com/awsutils/awsutils/archive/refs/heads/main.tar.gz}"
INSTALL_DIR="${AWSUTILS_INSTALL_DIR:-$HOME/.awsutils}"
ALIAS_FILE="${AWSUTILS_ALIAS_FILE:-$HOME/.aws/cli/alias}"

if ! command -v curl >/dev/null 2>&1; then
    printf '%s\n' "curl is required to download awsutils." >&2
    exit 1
fi

if ! command -v tar >/dev/null 2>&1; then
    printf '%s\n' "tar is required to unpack awsutils." >&2
    exit 1
fi

if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
else
    printf '%s\n' "python3 or python is required to run awsutils." >&2
    exit 1
fi

TMP_DIR="$(mktemp -d)"
cleanup() {
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT INT TERM

mkdir -p "$INSTALL_DIR"
curl -fsSL "$REPO_ARCHIVE_URL" -o "$TMP_DIR/awsutils.tar.gz"
tar -xzf "$TMP_DIR/awsutils.tar.gz" -C "$TMP_DIR"

SRC_DIR="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
rm -rf "$INSTALL_DIR/src"
mkdir -p "$INSTALL_DIR"
mv "$SRC_DIR" "$INSTALL_DIR/src"

mkdir -p "$(dirname "$ALIAS_FILE")"
touch "$ALIAS_FILE"

if ! grep -q '^\[toplevel\]' "$ALIAS_FILE"; then
    printf '\n[toplevel]\n' >>"$ALIAS_FILE"
fi

ALIAS_COMMAND="utils = !PYTHONPATH=$INSTALL_DIR/src $PYTHON_BIN -m awsutils.cli"

if grep -q '^utils = ' "$ALIAS_FILE"; then
    TMP_ALIAS="$TMP_DIR/alias"
    sed "s|^utils = .*|$ALIAS_COMMAND|" "$ALIAS_FILE" >"$TMP_ALIAS"
    mv "$TMP_ALIAS" "$ALIAS_FILE"
else
    printf '%s\n' "$ALIAS_COMMAND" >>"$ALIAS_FILE"
fi

printf '%s\n' "Installed awsutils to $INSTALL_DIR/src"
printf '%s\n' "Configured AWS CLI alias in $ALIAS_FILE"
printf '%s\n' "Run: aws utils hello"
