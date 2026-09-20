#!/bin/bash

set -euo pipefail

INSTALL_DIR=${AJIB_INSTALL_DIR:-/etc/ajib}
PYTHON="$INSTALL_DIR/ajib_venv/bin/python"
CLI="$INSTALL_DIR/core/cli.py"

if [ ! -x "$PYTHON" ]; then
    echo "ajib Python environment is missing. Run the installer or upgrade to repair it." >&2
    exit 1
fi

if [ "$#" -eq 0 ]; then
    exec "$INSTALL_DIR/menu.sh"
fi

exec "$PYTHON" "$CLI" "$@"
