# 五分钟 Docker Compose 部署

本教程适合一台全新的 Linux 云服务器。Docker Compose 会运行 RouteSnap API 和 Caddy；Caddy 自动申请并续期 HTTPS 证书。

## 1. 准备域名和端口

1. 在 DNS 控制台添加一条 A 记录，例如 `api.example.com` 指向服务器公网 IPv4。
2. 在腾讯云/阿里云等云防火墙或安全组放行 TCP 80、443。
3. 不要向公网开放 5001；它只供容器内部使用。

DNS 生效可以在电脑上检查：

```bash
nslookup api.example.com
```

## 2. 安装 Docker

按 [Docker Engine 官方安装文档](https://docs.docker.com/engine/install/)安装 Docker Engine 和 Compose 插件。安装后确认：

```bash
docker --version
docker compose version
```

## 3. 下载并填写配置

```bash
git clone YOUR_GITHUB_REPOSITORY_URL
cd routesnap
cp .env.example .env
openssl rand -hex 32
nano .env
```

填写示例：

```env
DEEPSEEK_API_KEY=sk-替换为你自己的密钥
DEEPSEEK_API_URL=https://api.deepseek.com/chat/completions
DEEPSEEK_MODEL=deepseek-v4-flash

AMAP_KEY=替换为你的高德Web服务Key
ROUTESNAP_ACCESS_TOKEN=替换为openssl生成的64位随机字符串
ROUTESNAP_DOMAIN=api.example.com

ROUTESNAP_HOST=0.0.0.0
ROUTESNAP_PORT=5001
ROUTESNAP_DATA_DIR=/data
XHS_FETCH_TIMEOUT=10
```

注意：

- `ROUTESNAP_DOMAIN` 只填域名，不带 `https://`，不带 `/parse`。
- `ROUTESNAP_ACCESS_TOKEN` 不是 DeepSeek Key，应独立随机生成。
- `.env` 已被 `.gitignore` 和 `.dockerignore` 排除，不要手动强制提交。

检查格式：

```bash
./scripts/check-config.sh .env
```

## 4. 启动

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f api
```

首次申请证书通常需要几秒到几分钟。查看 Caddy 日志：

```bash
docker compose logs -f caddy
```

## 5. 验证

```bash
curl https://api.example.com/health
```

正确响应应包含：

```json
{
  "status": "ok",
  "configured": true,
  "services": {
    "access_token": true,
    "amap": true,
    "deepseek": true
  }
}
```

再执行完整冒烟测试：

```bash
./scripts/smoke-test.sh https://api.example.com YOUR_ACCESS_TOKEN
```

## 6. 安装快捷指令

下载并打开 `shortcut/RouteSnap-Share.shortcut`，然后按 [快捷指令配置说明](SHORTCUT_SETUP.md)填写两组相同配置。

## 更新

```bash
git pull --ff-only
docker compose up -d --build
```

路线与 POI 缓存、Emoji 学习数据保存在 Docker 命名卷中，重新构建容器不会清空。

## 备份

查看数据卷名称：

```bash
docker volume ls | grep routesnap
```

至少备份 RouteSnap 数据卷。不要把 `.env` 放进公开备份；它包含所有密钥。
