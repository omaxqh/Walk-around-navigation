#!/usr/bin/env bash
set -euo pipefail

base_url="${1:-}"
access_token="${2:-}"

if [[ -z "$base_url" || -z "$access_token" ]]; then
  echo "用法：./scripts/smoke-test.sh https://api.example.com ACCESS_TOKEN" >&2
  exit 1
fi

base_url="${base_url%/}"

health_json="$(curl --fail --silent --show-error "$base_url/health")"
python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("status")=="ok", d; assert d.get("configured") is True, d' <<<"$health_json"

response_json="$(curl --fail --silent --show-error \
  "$base_url/parse" \
  -H "Authorization: Bearer $access_token" \
  -H "Content-Type: application/json" \
  --data '{"text":"路线：曲院风荷 → 杭州花圃 → 乌龟潭","mode":2}')"

python3 -c 'import json,sys; d=json.load(sys.stdin); assert d.get("success") is True, d; routes=d.get("routes") or []; assert routes and routes[0].get("amap_url", "").startswith("iosamap://"), d' <<<"$response_json"

echo "线上冒烟测试通过：健康检查、鉴权、解析和高德 URL 均正常。"
