#!/usr/bin/env sh
set -eu

REPO_ARCHIVE_URL="${AWSUTILS_ARCHIVE_URL:-https://github.com/awsutils/awsutils/archive/refs/heads/main.tar.gz}"
INSTALL_DIR="${AWSUTILS_INSTALL_DIR:-$HOME/.aws/cli/tools}"
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

ensure_section() {
    section="$1"
    if ! grep -q "^\[$section\]" "$ALIAS_FILE"; then
        printf '\n[%s]\n' "$section" >>"$ALIAS_FILE"
    fi
}

set_alias() {
    section="$1"
    alias_name="$2"
    alias_command="$3"
    ensure_section "$section"
    tmp_alias="$TMP_DIR/alias"
    awk -v section="[$section]" -v name="$alias_name" -v command="$alias_name = $alias_command" '
        BEGIN { in_section = 0; done = 0 }
        $0 == section { in_section = 1; print; next }
        /^\[/ {
            if (in_section && !done) { print command; done = 1 }
            in_section = 0
        }
        in_section && $0 ~ "^" name " = " {
            if (!done) { print command; done = 1 }
            next
        }
        { print }
        END { if (in_section && !done) print command }
    ' "$ALIAS_FILE" >"$tmp_alias"
    mv "$tmp_alias" "$ALIAS_FILE"
}

remove_alias() {
    section="$1"
    alias_name="$2"
    tmp_alias="$TMP_DIR/alias"
    awk -v section="[$section]" -v name="$alias_name" '
        $0 == section { in_section = 1; print; next }
        /^\[/ { in_section = 0 }
        in_section && $0 ~ "^" name " = " { next }
        { print }
    ' "$ALIAS_FILE" >"$tmp_alias"
    mv "$tmp_alias" "$ALIAS_FILE"
}

ALIAS_PREFIX="!PYTHONPATH=$INSTALL_DIR/src $PYTHON_BIN -m awsutils.cli"

remove_alias "toplevel" "utils"
set_alias "toplevel" "hello" "$ALIAS_PREFIX hello"
set_alias "toplevel" "backup" "$ALIAS_PREFIX backup"
set_alias "toplevel" "inspect" "$ALIAS_PREFIX inspect"
set_alias "toplevel" "vpc" "$ALIAS_PREFIX vpc"
set_alias "command cloudwatch" "create-dashboard" "$ALIAS_PREFIX cloudwatch create-dashboard"
set_alias "command logs" "create-fix-job" "$ALIAS_PREFIX logs create-fix-job"
set_alias "command logs" "describe-fix-job" "$ALIAS_PREFIX logs describe-fix-job"
set_alias "command s3" "create-log-bucket" "$ALIAS_PREFIX s3 create-log-bucket"
set_alias "command s3" "create-fix-job" "$ALIAS_PREFIX s3 create-fix-job"
set_alias "command s3" "describe-fix-job" "$ALIAS_PREFIX s3 describe-fix-job"

printf '%s\n' "Installed tools to $INSTALL_DIR/src"
printf '%s\n' "Configured AWS CLI alias in $ALIAS_FILE"
printf '%s\n' "Run: aws hello"
