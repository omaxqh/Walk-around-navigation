# 架构与数据流

## 组件

| 组件 | 职责 |
|---|---|
| iPhone 快捷指令 | 读取剪贴板、调用后端、必要时用手机网络获取正文、打开高德 URL |
| Flask / Gunicorn API | 鉴权、缓存、抓取、路线解析编排和错误处理 |
| DeepSeek / 兼容 LLM | 把正文转换为有序地点列表 |
| 高德 Web服务 API | POI 搜索、候选消歧和坐标获取 |
| SQLite | 持久化 POI 与正确路线缓存，重启后仍可秒出 |
| Caddy | HTTPS 证书和反向代理 |

## `/parse` 数据流

1. 校验 `Authorization: Bearer ...`。
2. 规范化输入并提取小红书短链。
3. 以“规范化链接/笔记标识 + 出行模式”查询路线缓存。
4. 未命中缓存时获取小红书正文：现代 iPhone Safari 标识优先，桌面 Chrome 标识只重试一次。
5. 若服务器抓取失败，返回 `xhs_fetch_failed`，快捷指令可携带 `source_text` 和 `source_url` 重试。
6. 先尝试确定性的快速路线解析；必要时调用 LLM。
7. 拒绝单独城市名作为具体 POI，调用高德进行候选召回、打分和坐标确认。
8. 保留 5 公里离群点静默删除规则。
9. 固定使用请求中的导航模式，POI 聚类不得改写步行模式。
10. 仅在完整正文解析成功且至少有两个有效地点时写入路线缓存。
11. 返回 `iosamap://path` URL，由快捷指令打开高德地图。

## 持久化目录

`ROUTESNAP_DATA_DIR` 包含：

- `cache.db`：POI 与路线缓存；
- `emoji_connector_library.json`：运行中学习到的 Emoji 连接符数据。

Docker 默认映射到 `/data` 命名卷；systemd 默认使用 `/var/lib/routesnap-share`。

## 安全边界

- DeepSeek 与高德 Key 只存在服务器环境变量中。
- 快捷指令持有后端 URL 和一个独立的共享访问 Token。
- `/` 和 `/health` 公开，但只返回布尔配置状态，不返回密钥内容。
- 其他接口都要求 Bearer Token。
- Gunicorn 不直接暴露公网端口，由 Caddy 终止 HTTPS。
