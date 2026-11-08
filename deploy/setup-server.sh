#!/usr/bin/env bash
# 服务器一次性初始化（Ubuntu/Debian，root 执行）。由 deploy.sh --setup 自动上传并运行，可重复执行。
# 做的事：装 Python venv、建 learnwise 用户和目录、装 systemd 服务、把站点接进现有 Caddy、装每日备份。
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE=/srv/learnwise

[ "$(id -u)" = 0 ] || { echo "请用 root 执行（sudo bash $0）"; exit 1; }

echo "==> 系统依赖"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-dev build-essential sqlite3 rsync curl >/dev/null

echo "==> 用户和目录 $BASE"
id learnwise &>/dev/null || useradd --system --home-dir "$BASE" --shell /usr/sbin/nologin learnwise
mkdir -p "$BASE/app" "$BASE/data" "$BASE/backup"
[ -d "$BASE/venv" ] || python3 -m venv "$BASE/venv"
# 国内服务器用腾讯云 PyPI 镜像
cat > "$BASE/pip.conf" <<PIPCONF
[global]
index-url = https://mirrors.cloud.tencent.com/pypi/simple
timeout = 60
PIPCONF
chown -R learnwise:learnwise "$BASE"
chmod 750 "$BASE/data" "$BASE/backup"

echo "==> systemd 服务"
install -m 644 "$HERE/learnwise.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable learnwise >/dev/null

echo "==> 每日备份"
install -m 755 "$HERE/learnwise-backup" /etc/cron.daily/learnwise-backup

echo "==> Caddy"
if [ -f /etc/caddy/Caddyfile ] && command -v caddy >/dev/null; then
  install -m 644 "$HERE/learnwise.caddy" /etc/caddy/learnwise.caddy
  if ! grep -q '^import learnwise.caddy' /etc/caddy/Caddyfile; then
    cp /etc/caddy/Caddyfile "/etc/caddy/Caddyfile.bak.$(date +%Y%m%d%H%M%S)"
    printf '\nimport learnwise.caddy\n' >> /etc/caddy/Caddyfile
  fi
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
  systemctl reload caddy
  echo "Caddy 已加载 learnwise.waywise.tech"
else
  echo "!! 没找到 /etc/caddy/Caddyfile，请手动把 $HERE/learnwise.caddy 加进 Caddy 配置后 reload。"
fi
echo "初始化完成。"
