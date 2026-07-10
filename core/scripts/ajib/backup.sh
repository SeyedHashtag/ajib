#!/bin/bash

BACKUP_DIR="/opt/hysbackup"
BACKUP_FILE="$BACKUP_DIR/ajib_backup_$(date +%Y%m%d_%H%M%S).zip"

if [ ! -d "$BACKUP_DIR" ]; then
    mkdir -p "$BACKUP_DIR"
fi

shopt -s nullglob dotglob
FILES_TO_BACKUP=(
    /etc/ajib/*.env
    /etc/ajib/*.json
    /etc/ajib/core/scripts/telegrambot/*.env
    /etc/ajib/core/scripts/telegrambot/*.json
)
shopt -u nullglob dotglob

RELATIVE_FILES=()
for FILE in "${FILES_TO_BACKUP[@]}"; do
    if [ -f "$FILE" ]; then
        RELATIVE_FILES+=("${FILE#/etc/ajib/}")
    fi
done

if [ ${#RELATIVE_FILES[@]} -eq 0 ]; then
    echo "Backup failed!"
    exit 1
fi

(
    cd /etc/ajib || exit 1
    zip "$BACKUP_FILE" "${RELATIVE_FILES[@]}" >/dev/null
)

if [ $? -eq 0 ]; then
    echo "Backup successfully created"
else
    echo "Backup failed!"
fi
