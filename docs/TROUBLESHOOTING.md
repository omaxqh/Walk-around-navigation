# 故障排查

先运行：

```bash
curl https://api.example.com/health
./scripts/smoke-test.sh https://api.example.com YOUR_ACCESS_TOKEN
```

Docker 查看日志：

```bash
docker compose ps
docker compose logs --tail=200 api
docker compose logs --tail=200 caddy
```

systemd 查看日志：

```bash
sudo systemctl status routesnap-share
sudo journalctl -u routesnap-share -n 200 --no-pager
```

## `configured: false`

查看响应中的 `missing`，然后检查 `.env`：

```bash
./scripts/check-config.sh .env
```

`access_token` 为 false 通常表示 Token 少于 32 个字符。

## HTTP 401 `unauthorized`

快捷指令中的请求头和服务器 Token 不一致。正确格式：

```text
Authorization: Bearer 你的ROUTESNAP_ACCESS_TOKEN
```

修改 Token 后，主请求和手机正文重试请求都要同步修改。

## HTTP 502 / 域名打不开

- 检查 RouteSnap API 是否健康。
- 检查 Caddy 是否能连接 API。
- 确认 DNS 指向当前服务器。
- 确认云安全组放行 TCP 80、443。
- 不要把快捷指令地址写成内部端口 `:5001`。

## DeepSeek 401、402、429 或 503

- 401：API Key 错误或已撤销。
- 402：余额不足。
- 429：调用频率或配额限制。
- 503：上游临时不可用，可稍后重试。

确认模型名和接口地址匹配供应商。DeepSeek 默认配置为：

```env
DEEPSEEK_API_URL=https://api.deepseek.com/chat/completions
DEEPSEEK_MODEL=deepseek-v4-flash
```

## 高德错误

- `10001`：Key 不正确。
- `10005`：服务器出口 IP 不在高德白名单。
- `10009`：错误地申请了 JS API、Android 或 iOS Key；应申请“Web服务”Key。

## 小红书 `300011` / `xhs_fetch_failed`

服务器会先用现代 iPhone Safari 标识请求，明确失败后再用桌面 Chrome 标识重试一次。仍失败时，快捷指令才尝试在 iPhone 网络获取正文。

这是上游页面访问限制，不要把分享口令中链接前的截断标题交给 AI 猜路线。当前后端会阻止这种错误降级，也不会缓存失败结果。

## 快捷指令提示“未指定 URL”

通常是导入时没有正确填写完整 `/parse` 地址，或后端返回失败结果后没有 `amap_url`。依次检查：

1. 两处 URL 都是 `https://你的域名/parse`。
2. 两处 Authorization 都正确。
3. `/health` 为 configured true。
4. 冒烟测试通过。

## 提示无法为 `iosamap` 打开 App

确认 iPhone 安装了高德地图。`iosamap://` 是高德 App 的 URL Scheme，Mac 没有安装高德 App 时无法打开；本项目目标是手机端路线展示。

## 路线漏点

项目会静默删除与最近邻超过 5 公里的离群点，这是保留的产品规则。其他漏点可从 API 日志判断是正文没取得、LLM 未提取，还是高德 POI 匹配失败。
