#!/usr/bin/env bash
# fris 一键发布：本机打包 → 上传 → 服务器备份旧版 → 解压 → 重启 → 验证
# 用法：bash scripts/deploy.sh            （会提示输入服务器密码）
#      FRIS_SERVER=root@1.2.3.4 bash scripts/deploy.sh
set -euo pipefail

SERVER="${FRIS_SERVER:-}"
DIR="${FRIS_DIR:-/opt/fris}"
if [ -z "$SERVER" ]; then
  echo "用法：FRIS_SERVER=root@<your-server> [FRIS_DIR=/opt/fris] bash scripts/deploy.sh" >&2
  exit 2
fi
TS="$(date +%Y%m%d-%H%M%S)"
PKG="/tmp/fris-deploy-${TS}.tgz"
REPO="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> 1/5 打包源码（排除 .git/缓存/数据/凭证）"
tar czf "$PKG" -C "$REPO" \
  --exclude='.git' \
  --exclude='__pycache__' \
  --exclude='backups' \
  --exclude='archive*.db*' \
  --exclude='nohup.out' \
  --exclude='workbench/config.json' \
  --exclude='workbench/.tqenv' \
  .
echo "    $(du -h "$PKG" | cut -f1)  $PKG"

echo "==> 2/5 上传到 $SERVER:$DIR"
scp "$PKG" "$SERVER:$DIR/"

echo "==> 3/5 服务器备份旧版代码（保留最近 10 份）"
ssh "$SERVER" "cd $DIR && mkdir -p releases && \
  tar czf releases/code-${TS}.tgz --exclude='.venv' --exclude='backups' --exclude='archive*.db*' --exclude='releases' . && \
  ls -1t releases/code-*.tgz | tail -n +11 | xargs -r rm -f && echo '    备份 releases/code-${TS}.tgz'"

echo "==> 4/5 解压并重启服务"
ssh "$SERVER" "cd $DIR && tar xzf $(basename "$PKG") && rm -f $(basename "$PKG") && systemctl restart fris"

echo "==> 5/5 验证"
sleep 8
ssh "$SERVER" "systemctl is-active fris; tail -6 /var/log/fris.log"
curl -s -o /dev/null -w "    HTTP %{http_code}  http://${SERVER#*@}:${HTTP_PORT:-8000}/\n" --max-time 10 "http://${SERVER#*@}:${HTTP_PORT:-8000}/"

echo "==> 发布完成：$TS"
