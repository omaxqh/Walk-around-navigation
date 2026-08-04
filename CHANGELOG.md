# 更新记录

## 1.0.0 - 2026-08-04

- 提供完全自托管的 RouteSnap 后端与 Apple 签名快捷指令文件。
- 支持每位使用者配置自己的 DeepSeek/OpenAI 兼容 LLM、高德 Web服务 Key 和访问 Token。
- 提供 Docker Compose 自动 HTTPS 部署，以及 Ubuntu systemd + Caddy 部署。
- 路线与 POI 缓存持久化到 SQLite，服务重启后保留。
- 小红书服务器抓取失败时支持 iPhone 本机正文重试，失败不再用截断标题猜路线。
- 固定手机端请求的导航模式，并保留 5 公里离群点静默删除规则。
