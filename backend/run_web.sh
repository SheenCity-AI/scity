#!/bin/bash
# 启动网页后台，只监听本机 8000 端口，由 nginx 转发 /api/。
cd "$(dirname "$0")"
mkdir -p logs && chmod 700 logs
LOGGING_LEVEL=ERROR env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
  ./.venv/bin/python -u -m scity.web 2>&1 \
  | sed -u -E 's/(access_key|ticket|app_secret|client_secret|token|code)=[^& ]+/\1=***/gI' >> logs/web.log
