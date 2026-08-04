# 参与开发

## 本地环境

需要 Python 3.11 或 3.12：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest -v test_mobile_amap_only.py
```

单元测试必须使用 mock，不应调用真实 DeepSeek、高德或小红书服务。

## 提交前检查

```bash
./scripts/verify-release.sh
```

请确认：

- 没有 `.env`、数据库、日志或缓存文件；
- 没有真实 API Key、访问 Token或原作者服务器地址；
- 快捷指令仍是 Apple 签名导出格式；
- 新行为有回归测试；
- README 与 `docs/` 中的配置名称和代码一致。

## 修改快捷指令

1. 在 Apple“快捷指令”App中复制一个工作副本。
2. 两个 `/parse` 请求必须同步修改。
3. 导出时选择“任何人”，保存到 `shortcut/漫步导航 分享版.shortcut`。
4. 导出前确认 URL 和 Authorization 都是占位值，不能包含维护者真实服务信息。
