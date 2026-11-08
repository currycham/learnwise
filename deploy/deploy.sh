#!/usr/bin/env bash
# 在本机执行（fixtures/ 是生成学习页时给模型看的样例，要一起部署），把当前代码部署到服务器并重启服务。
#
#   DEPLOY_HOST=ubuntu@<服务器IP> DEPLOY_KEY=~/.ssh/<你的密钥> deploy/deploy.sh --setup --env .env   # 第一次
#   DEPLOY_HOST=ubuntu@<服务器IP> DEPLOY_KEY=~/.ssh/<你的密钥> deploy/deploy.sh                      # 以后每次更新
#
# 选项：
#   --setup      先运行 setup-server.sh（装 Python venv、建用户、systemd、Caddy 配置），可重复执行
#   --env FILE   上传 FILE 作为服务器上的 .env
#
# 要求：本机能 ssh 免密登录 DEPLOY_HOST，且该用户有免密 sudo（腾讯云的 ubuntu 默认满足）。
set -euo pipefail
cd "$(dirname "$0")/.."

HOST="${DEPLOY_HOST:-}"
[ -n "$HOST" ] || { echo "请设置 DEPLOY_HOST，例如 DEPLOY_HOST=ubuntu@<服务器IP>"; exit 1; }
SETUP=0; ENV_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --setup) SETUP=1 ;;
    --env) ENV_FILE="$2"; shift ;;
    *) echo "未知选项 $1"; exit 1 ;;
  esac
  shift
done

BASE=/srv/learnwise
SSH_CMD="ssh -o ServerAliveInterval=30${DEPLOY_KEY:+ -i $DEPLOY_KEY -o IdentitiesOnly=yes}"
SSH="$SSH_CMD $HOST"
export RSYNC_RSH="$SSH_CMD"
RSYNC_SUDO=(--rsync-path="sudo rsync")

if [ -n "$(git status --porcelain 2>/dev/null)" ]; then echo "提示：有未提交的改动，也会一起部署。"; fi

if [ "$SETUP" = 1 ]; then
  echo "==> 初始化服务器"
  rsync -az --delete deploy/ "$HOST:/tmp/learnwise-deploy/"
  $SSH "sudo bash /tmp/learnwise-deploy/setup-server.sh"
fi

echo "==> 同步代码"
rsync -az --delete "${RSYNC_SUDO[@]}" \
  --include '.env.example' --exclude '.env' --exclude '.env.*' \
  --exclude '.git/' --exclude '.venv/' --exclude 'data/' --exclude 'out/' --exclude 'node_modules/' --exclude '__pycache__/' --exclude '.DS_Store' \
  ./ "$HOST:$BASE/app/"

if [ -n "$ENV_FILE" ]; then
  echo "==> 上传 $ENV_FILE 为服务器 .env"
  rsync -az "${RSYNC_SUDO[@]}" "$ENV_FILE" "$HOST:$BASE/app/.env"
fi
if ! $SSH "sudo test -f $BASE/app/.env"; then
  echo "!! 服务器上还没有 .env：DEPLOY_HOST=$HOST deploy/deploy.sh --env 你的.env"
  exit 1
fi
$SSH "sudo chown -R learnwise:learnwise $BASE/app $BASE/data && sudo chmod 600 $BASE/app/.env"

echo "==> 安装依赖"
$SSH "sudo -u learnwise -H env PIP_CONFIG_FILE=$BASE/pip.conf $BASE/venv/bin/pip install -q --disable-pip-version-check -r $BASE/app/requirements.txt"

echo "==> 重启服务"
$SSH "sudo systemctl restart learnwise"

echo "==> 检查"
for i in $(seq 1 20); do
  if $SSH "curl -fsS -o /dev/null http://127.0.0.1:8400/" 2>/dev/null; then
    echo "网站已启动：https://learnwise.waywise.tech"
    echo "看日志：ssh $HOST sudo journalctl -u learnwise -f"
    exit 0
  fi
  sleep 2
done
echo "!! 网站 40 秒内没有响应，看日志：ssh $HOST sudo journalctl -u learnwise -n 100"
exit 1
