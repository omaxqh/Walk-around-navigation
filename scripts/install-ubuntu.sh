#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "请使用 sudo 运行：sudo ./scripts/install-ubuntu.sh" >&2
  exit 1
fi

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
app_dir="/opt/routesnap-share"
env_file="/etc/routesnap-share.env"
service_file="/etc/systemd/system/routesnap-share.service"

apt-get update
apt-get install -y python3 python3-venv python3-pip ca-certificates curl

if ! id routesnap >/dev/null 2>&1; then
  useradd --system --home-dir "$app_dir" --shell /usr/sbin/nologin routesnap
fi

install -d -o routesnap -g routesnap -m 0750 "$app_dir" "$app_dir/config"
install -m 0644 "$repo_dir/app.py" "$repo_dir/emoji_learner.py" "$repo_dir/poi_disambiguate.py" "$repo_dir/requirements.txt" "$app_dir/"
cp -a "$repo_dir/config/." "$app_dir/config/"
chown -R routesnap:routesnap "$app_dir"

python3 -m venv "$app_dir/venv"
"$app_dir/venv/bin/pip" install --upgrade pip
"$app_dir/venv/bin/pip" install -r "$app_dir/requirements.txt"
chown -R routesnap:routesnap "$app_dir/venv"

if [[ ! -e "$env_file" ]]; then
  install -o root -g root -m 0600 "$repo_dir/.env.example" "$env_file"
  echo "已创建 $env_file；请填写密钥和域名。"
else
  echo "保留现有 $env_file，未覆盖任何密钥。"
fi

install -o root -g root -m 0644 "$repo_dir/deploy/routesnap-share.service" "$service_file"
systemctl daemon-reload
systemctl enable routesnap-share.service

echo
echo "应用文件已安装，但尚未自动启动。下一步："
echo "1. sudo nano $env_file"
echo "2. sudo $repo_dir/scripts/check-config.sh $env_file"
echo "3. sudo systemctl restart routesnap-share"
echo "4. 按 docs/SERVER_SETUP.md 配置 Caddy 与 HTTPS"
