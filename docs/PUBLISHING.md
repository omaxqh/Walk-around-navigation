# 发布到 GitHub

## 发布前检查

```bash
./scripts/verify-release.sh
git status --short
git check-ignore .env cache.db .runtime/
```

确认：

- 仓库中没有 `.env`、SQLite 数据库、日志和 `__pycache__`；
- 没有原作者服务器地址或真实 API Key；
- `shortcut/RouteSnap-Share.shortcut` 是未配置个人服务器的 Apple 签名分享文件；
- README 的快捷指令、部署与故障排查链接都能打开；
- GitHub Actions 测试通过。

## 创建远程仓库

在 GitHub 创建一个空仓库，不要勾选自动生成 README、`.gitignore` 或许可证，然后在本地执行：

```bash
git remote add origin git@github.com:YOUR_NAME/YOUR_REPOSITORY.git
git push -u origin main
```

如果使用 HTTPS remote，也不要把 GitHub Token 写进 remote URL、脚本或文档。

## 许可证

本发布包没有替维护者擅自选择开源许可证。公开发布前，请根据你希望别人如何使用、修改和再分发代码选择许可证，例如常见的 MIT、Apache-2.0 或 GPL-3.0，并把对应 `LICENSE` 文件加入仓库。

在没有许可证时，GitHub 可以展示源码，但他人对复制、修改和再发布代码的法律权限并不明确。

## GitHub Release

建议创建 `v1.0.0` Release，并附上：

- 仓库源码压缩包；
- `shortcut/RouteSnap-Share.shortcut`；
- `shortcut/SHA256SUMS`；
- 指向 `docs/QUICKSTART.md` 的安装说明。

不要上传已经填入真实后端 Token 的快捷指令副本。
