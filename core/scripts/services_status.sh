#!/bin/bash

if systemctl is-active --quiet ajib-telegram-bot.service; then
    echo '{"ajib-telegram-bot.service":true}'
else
    echo '{"ajib-telegram-bot.service":false}'
fi
