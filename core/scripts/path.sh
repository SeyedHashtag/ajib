#!/bin/bash
# shellcheck disable=SC2034  # Variables are consumed by scripts that source this file.

AJIB_INSTALL_DIR=${AJIB_INSTALL_DIR:-/etc/ajib}
CLI_PATH="$AJIB_INSTALL_DIR/core/cli.py"
AJIB_PYTHON="$AJIB_INSTALL_DIR/ajib_venv/bin/python"
TELEGRAM_ENV="$AJIB_INSTALL_DIR/core/scripts/telegrambot/.env"
LOCALVERSION="$AJIB_INSTALL_DIR/VERSION"
LATESTVERSION="https://raw.githubusercontent.com/SeyedHashtag/ajib/main/VERSION"
LASTESTCHANGE="https://raw.githubusercontent.com/SeyedHashtag/ajib/main/changelog"
