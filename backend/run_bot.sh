#!/bin/bash
# 启动飞书机器人。直连飞书和方舟（不走本机代理），日志里的连接参数打码。
cd "$(dirname "$0")"
mkdir -p logs && chmod 700 logs
LOGGING_LEVEL=ERROR env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY -u NODE_USE_ENV_PROXY \
  ./.venv/bin/python -u -m scity.feishu_bot 2>&1 \
  | sed -u -E 's/(access_key|ticket|app_secret|token)=[^& ]+/\1=***/gI' >> logs/bot.log
