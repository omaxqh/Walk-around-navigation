# Ubuntu + systemd + Caddy 部署

这是不使用 Docker 的部署方式。示例面向 Ubuntu 22.04/24.04，应用安装到 `/opt/routesnap-share`，配置放在 `/etc/routesnap-share.env`，运行数据放在 `/var/lib/routesnap-share`。

## 1. 安装应用

```bash
git clone YOUR_GITHUB_REPOSITORY_URL
cd routesnap
sudo ./scripts/install-ubuntu.sh
```

安装脚本会：

- 创建无登录权限的 `routesnap` 系统用户；
- 创建 Python 虚拟环境并安装依赖；
- 安装强化过的 systemd 服务；
- 首次安装时创建权限为 `0600` 的 `/etc/routesnap-share.env`；
- 保留已经存在的配置文件，不覆盖密钥；
- 启用服务，但在你配置密钥前不自动启动。

## 2. 配置密钥

```bash
openssl rand -hex 32
sudo nano /etc/routesnap-share.env
sudo ./scripts/check-config.sh /etc/routesnap-share.env
sudo systemctl restart routesnap-share
sudo systemctl status routesnap-share
```

本机检查：

```bash
curl http://127.0.0.1:5001/health
```

## 3. 安装 Caddy

按照 [Caddy 官方 Debian/Ubuntu 安装说明](https://caddyserver.com/docs/install#debian-ubuntu-raspbian)安装稳定版：

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
sudo apt update
sudo apt install caddy
```

如果服务器没有其他 Caddy 站点，可以使用仓库模板。先备份现有配置，再替换示例域名：

```bash
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.backup
sudo cp deploy/Caddyfile.example /etc/caddy/Caddyfile
sudo sed -i 's/api.example.com/你的真实域名/' /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

如果 Caddy 已承载其他网站，不要覆盖整个文件；只把 `deploy/Caddyfile.example` 的站点块合并进去。

## 4. 云防火墙

- 入站允许 TCP 80、443。
- 5001 不对公网放行。
- 出站允许访问 `api.deepseek.com`、`restapi.amap.com` 和小红书分享域名。

## 5. 验证

```bash
curl https://你的真实域名/health
./scripts/smoke-test.sh https://你的真实域名 YOUR_ACCESS_TOKEN
```

## 日志与更新

```bash
sudo journalctl -u routesnap-share -f
git pull --ff-only
sudo ./scripts/install-ubuntu.sh
sudo systemctl restart routesnap-share
```

重复运行安装脚本不会覆盖 `/etc/routesnap-share.env`。
