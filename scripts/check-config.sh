#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"

if [[ ! -r "$env_file" ]]; then
  echo "错误：无法读取配置文件 $env_file" >&2
  exit 1
fi

read_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "$env_file" | tail -n 1
}

failed=0

require_value() {
  local key="$1"
  local value
  value="$(read_value "$key")"
  if [[ -z "$value" || "$value" == replace_* || "$value" == *"请"* ]]; then
    echo "缺少或未替换：$key" >&2
    failed=1
  else
    echo "已配置：$key"
  fi
}

require_value DEEPSEEK_API_KEY
require_value DEEPSEEK_API_URL
require_value DEEPSEEK_MODEL
require_value AMAP_KEY
require_value ROUTESNAP_ACCESS_TOKEN
require_value ROUTESNAP_DOMAIN

token="$(read_value ROUTESNAP_ACCESS_TOKEN)"
if [[ -n "$token" && ${#token} -lt 32 ]]; then
  echo "ROUTESNAP_ACCESS_TOKEN 至少需要 32 个字符" >&2
  failed=1
fi

api_url="$(read_value DEEPSEEK_API_URL)"
if [[ -n "$api_url" && ! "$api_url" =~ ^https:// ]]; then
  echo "DEEPSEEK_API_URL 必须使用 https://" >&2
  failed=1
fi

domain="$(read_value ROUTESNAP_DOMAIN)"
if [[ -n "$domain" && ! "$domain" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "ROUTESNAP_DOMAIN 只能填写域名，不能带 https:// 或路径" >&2
  failed=1
fi

if [[ $failed -ne 0 ]]; then
  exit 1
fi

echo "配置格式检查通过（未显示任何密钥内容）。"
