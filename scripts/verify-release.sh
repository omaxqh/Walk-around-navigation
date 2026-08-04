#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_dir"

required=(
  README.md
  app.py
  poi_disambiguate.py
  emoji_learner.py
  requirements.txt
  .env.example
  compose.yaml
  Dockerfile
  "shortcut/漫步导航 分享版.shortcut"
  shortcut/SHA256SUMS
)

for path in "${required[@]}"; do
  [[ -f "$path" ]] || { echo "缺少发布文件：$path" >&2; exit 1; }
done

[[ ! -e .env ]] || { echo "发布目录不能包含 .env" >&2; exit 1; }
[[ ! -e cache.db ]] || { echo "发布目录不能包含 cache.db" >&2; exit 1; }

if find . -path './.git' -prune -o -type d -name __pycache__ -print | grep -q .; then
  echo "发布目录包含 __pycache__" >&2
  exit 1
fi

if grep -RInE --exclude='*.shortcut' --exclude-dir='.git' '49\.232\.142\.84|sk-[A-Za-z0-9_-]{16,}' .; then
  echo "检测到原服务器地址或疑似真实 API Key" >&2
  exit 1
fi

shortcut_magic="$(od -An -tx1 -N4 'shortcut/漫步导航 分享版.shortcut' | tr -d ' \n')"
[[ "$shortcut_magic" == "41454131" ]] || { echo "快捷指令不是 Apple 签名导出格式" >&2; exit 1; }

expected_shortcut_hash="$(awk '{print $1}' shortcut/SHA256SUMS)"
actual_shortcut_hash="$(python3 -c 'import hashlib; print(hashlib.sha256(open("shortcut/漫步导航 分享版.shortcut", "rb").read()).hexdigest())')"
[[ "$actual_shortcut_hash" == "$expected_shortcut_hash" ]] || { echo "快捷指令校验和不匹配" >&2; exit 1; }

python3 -c 'from pathlib import Path; [compile(path.read_text(encoding="utf-8"), str(path), "exec") for path in map(Path, ("app.py", "poi_disambiguate.py", "emoji_learner.py"))]'
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -q test_mobile_amap_only.py

if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  trap 'rm -f .env .env.bak' EXIT
  cp .env.example .env
  sed -i.bak 's/replace_with_your_deepseek_api_key/test-key/; s/replace_with_your_amap_web_service_key/test-key/; s/replace_with_a_long_random_token/0123456789abcdef0123456789abcdef/' .env
  docker compose config >/dev/null
  rm -f .env .env.bak
  trap - EXIT
fi

echo "发布包验证通过。"
