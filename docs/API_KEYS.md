# DeepSeek 与高德 API Key 配置

## DeepSeek

1. 登录 [DeepSeek 开放平台](https://platform.deepseek.com/)。
2. 在 [API Keys](https://platform.deepseek.com/api_keys) 创建新密钥并充值余额。
3. 只把密钥写入服务器 `.env` 或 `/etc/routesnap-share.env`：

```env
DEEPSEEK_API_KEY=sk-你的密钥
DEEPSEEK_API_URL=https://api.deepseek.com/chat/completions
DEEPSEEK_MODEL=deepseek-v4-flash
```

DeepSeek 当前官方文档列出的模型包含 `deepseek-v4-flash` 和 `deepseek-v4-pro`。路线提取以速度和成本优先，默认使用 `deepseek-v4-flash`。接口采用 OpenAI 兼容的 Chat Completions 响应格式。

也可以使用其他供应商，但必须同时满足：

- 支持 `POST /chat/completions`；
- 使用 `Authorization: Bearer ...`；
- 返回 `choices[0].message.content`；
- 能稳定输出 JSON。

## 高德开放平台

1. 登录 [高德开放平台控制台](https://console.amap.com/dev/key/app)。
2. 创建应用。
3. 添加 Key 时，“服务平台”必须选择 **Web服务**，不能选 Web端(JS API)、Android 或 iOS。
4. 将 Key 写入服务器：

```env
AMAP_KEY=你的Web服务Key
```

官方说明：[申请 Web服务 API Key](https://lbs.amap.com/api/webservice/guide/create-project/get-key)。

### IP 白名单

建议在高德控制台为 Key 设置服务器公网出口 IP 白名单。白名单必须填实际发出请求的公网 IP；云服务器使用 NAT 时，它可能和内网 IP 不同。

常见错误：

- `10001 INVALID_USER_KEY`：Key 不正确或已失效。
- `10005 INVALID_USER_IP`：服务器出口 IP 不在白名单。
- `10009 USERKEY_PLAT_NOMATCH`：申请的不是“Web服务”类型 Key。
- `10003 DAILY_QUERY_OVER_LIMIT`：超过日配额。

错误码可查 [高德 Web服务 API 官方说明](https://lbs.amap.com/api/webservice/guide/tools/info/)。

## 三种密钥不要混用

| 配置 | 用途 | 保存位置 |
|---|---|---|
| `DEEPSEEK_API_KEY` | LLM 提取路线 | 仅服务器 |
| `AMAP_KEY` | POI 搜索和地理编码 | 仅服务器 |
| `ROUTESNAP_ACCESS_TOKEN` | 保护你自己的 `/parse` 接口 | 服务器和你的快捷指令 |

如果任何真实 Key 曾提交到 GitHub，仅删除文件不够；应立即在对应平台撤销并重新生成。
