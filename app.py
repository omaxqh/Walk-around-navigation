"""
RouteSnap 后端服务
功能：解析小红书帖子内容/链接，提取路线，生成高德导航URL
"""
import os
import hmac
import shutil

from flask import Flask, request, jsonify
import requests
import json
import re
import sqlite3
import time
import urllib.parse
import urllib3
from bs4 import BeautifulSoup

# POI 消歧模块（与分享版后端放在同一目录）
from poi_disambiguate import disambiguate_route, batch_geocode, geocode, clear_cache, get_cache_stats, build_amap_url as poi_build_amap_url, haversine, cluster_pois_by_distance, sort_pois_nearest_neighbor

# Emoji 自学习模块
from emoji_learner import load_emoji_library, extract_emojis, find_unknown_emojis, label_emojis_retroactively, learn_emoji, process_text_for_learning

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)

# ==================== 配置 ====================

APP_VERSION = "1.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, ".runtime")
DATA_DIR = os.environ.get("ROUTESNAP_DATA_DIR", DEFAULT_DATA_DIR).strip() or DEFAULT_DATA_DIR
os.makedirs(DATA_DIR, exist_ok=True)

# DeepSeek API（OpenAI 兼容格式）
DEEPSEEK_API_URL = os.environ.get(
    "DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions"
)
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
ROUTESNAP_ACCESS_TOKEN = os.environ.get("ROUTESNAP_ACCESS_TOKEN", "").strip()


def configuration_status():
    """返回不包含密钥内容的配置状态。"""
    configured = {
        "deepseek": bool(DEEPSEEK_API_KEY),
        "amap": bool(os.environ.get("AMAP_KEY", "").strip()),
        "access_token": len(ROUTESNAP_ACCESS_TOKEN) >= 32,
    }
    return {
        "configured": all(configured.values()),
        "services": configured,
        "missing": [name for name, ready in configured.items() if not ready],
    }


@app.before_request
def require_access_token():
    """分享版默认关闭匿名调用，只公开服务说明与健康检查。"""
    if request.path in ("/", "/health"):
        return None
    if len(ROUTESNAP_ACCESS_TOKEN) < 32:
        return jsonify({
            "success": False,
            "error_code": "server_not_configured",
            "error": "服务器未配置至少32字符的 ROUTESNAP_ACCESS_TOKEN",
        }), 503
    authorization = request.headers.get("Authorization", "")
    prefix = "Bearer "
    provided = authorization[len(prefix):].strip() if authorization.startswith(prefix) else ""
    if not provided or not hmac.compare_digest(provided, ROUTESNAP_ACCESS_TOKEN):
        return jsonify({
            "success": False,
            "error_code": "unauthorized",
            "error": "访问令牌无效",
        }), 401
    return None

# 高德 API Key（POI消歧模块使用，此处保留供参考）
# AMAP_KEY 已在 poi_disambiguate.py 中配置

import hashlib

# Emoji 自学习系统
DEFAULT_EMOJI_LIBRARY_PATH = os.path.join(
    BASE_DIR,
    "config",
    "emoji_connector_library.json",
)
EMOJI_LIBRARY_PATH = os.path.join(DATA_DIR, "emoji_connector_library.json")
if not os.path.exists(EMOJI_LIBRARY_PATH):
    shutil.copyfile(DEFAULT_EMOJI_LIBRARY_PATH, EMOJI_LIBRARY_PATH)
EMOJI_LIBRARY = load_emoji_library(EMOJI_LIBRARY_PATH)
EMOJI_LEARN_THRESHOLD = 0.80

# 路线级缓存：内存作为 L1，SQLite 作为跨重启的 L2。
_route_cache = {}
_ROUTE_CACHE_DB = os.path.join(DATA_DIR, "cache.db")

_xhs_fetch_stats = {
    "primary_success": 0,
    "fallback_success": 0,
    "error_300011": 0,
    "failures": 0,
    "persistent_cache_hits": 0,
    "blocked_title_fallbacks": 0,
}

XHS_MOBILE_USER_AGENT = os.environ.get(
    "XHS_MOBILE_USER_AGENT",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 "
    "Mobile/15E148 Safari/604.1",
)
XHS_DESKTOP_USER_AGENT = os.environ.get(
    "XHS_DESKTOP_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
)
XHS_FETCH_TIMEOUT = float(os.environ.get("XHS_FETCH_TIMEOUT", "10"))

def _route_cache_key(text: str, mode: int, city: str = "") -> str:
    """生成路线缓存 key"""
    normalized = normalize_connectors(text).strip().lower()
    text_hash = hashlib.md5(normalized.encode()).hexdigest()[:16]
    return f"route:{city}:{text_hash}:{mode}"


def _route_source_cache_key(url: str, mode: int, city: str = "") -> str:
    """按小红书短链/笔记链接生成可在抓取前查询的稳定缓存 key。"""
    parsed = urllib.parse.urlsplit(url.strip())
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    source_hash = hashlib.sha256(f"{host}{path}".encode()).hexdigest()[:24]
    return f"route:xhs:{city.strip().lower()}:{source_hash}:{mode}"


def _persistent_route_cache_get(key: str):
    try:
        conn = sqlite3.connect(_ROUTE_CACHE_DB)
        row = conn.execute("SELECT value FROM route_cache WHERE key = ?", (key,)).fetchone()
        if row:
            conn.execute(
                "UPDATE route_cache SET hit_count = hit_count + 1 WHERE key = ?", (key,)
            )
            conn.commit()
            value = json.loads(row[0])
            conn.close()
            return value
        conn.close()
    except Exception as exc:
        print(f"[路线缓存] SQLite 读取失败: {exc}")
    return None


def _persistent_route_cache_set(key: str, value: dict):
    try:
        conn = sqlite3.connect(_ROUTE_CACHE_DB)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS route_cache (
                key TEXT PRIMARY KEY,
                value TEXT,
                created_at REAL,
                hit_count INTEGER DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO route_cache (key, value, created_at, hit_count) "
            "VALUES (?, ?, ?, 0)",
            (key, json.dumps(value, ensure_ascii=False), time.time()),
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        print(f"[路线缓存] SQLite 写入失败: {exc}")


def _get_route_cache(key: str):
    cached = _route_cache.get(key)
    if cached is not None:
        return cached
    cached = _persistent_route_cache_get(key)
    if cached is not None:
        _route_cache[key] = cached
        _xhs_fetch_stats["persistent_cache_hits"] += 1
    return cached


def _is_cacheable_route_response(response_data: dict) -> bool:
    if not response_data.get("success"):
        return False
    for route in response_data.get("routes", []):
        points = route.get("points") or route.get("locations") or []
        if route.get("amap_url") and len(points) >= 2:
            return True
    return False


def _set_route_cache(key: str, response_data: dict):
    """只保存由完整正文生成且包含有效路线的成功响应。"""
    if not key or not _is_cacheable_route_response(response_data):
        return
    _route_cache[key] = response_data
    _persistent_route_cache_set(key, response_data)

# ==================== 连接符号预处理 ====================

# 高置信度箭头符号 - 统一替换为标准箭头 →
ARROW_CONNECTORS = [
    "➡️", "➜", "➔", "➝", "➞", "➟", "➠", "➡", "⇒", "⇨", "⇰",
    "👉", "👈", "☞", "☛",
    "▶️", "▷", "►", "⏩",
    "➤", "➢", "➣", "➥", "➦",
    "⟶", "⟹", "⟼", "↦", "↣", "↠", "↬", "↷",
    "➩", "➪", "➫", "➬", "➭", "➮", "➯", "➱"
]

def _load_known_connectors() -> list:
    """从 emoji 库中加载所有已知连接符（静态 + 学到的）"""
    connectors = list(ARROW_CONNECTORS)  # 原有硬编码列表
    # 追加学到的连接符
    learned = EMOJI_LIBRARY.get("learned", {})
    for emoji, info in learned.items():
        if isinstance(info, dict) and info.get("role") == "connector" and info.get("confidence", 0) >= 0.80:
            if emoji not in connectors:
                connectors.append(emoji)
    return connectors


def normalize_connectors(text: str) -> str:
    """
    将各种箭头emoji统一替换为标准箭头 →
    这样AI更容易识别路线连接关系
    动态加载：包含 ARROW_CONNECTORS + 学到的高置信度连接符
    """
    result = text
    known_connectors = _load_known_connectors()
    for arrow in known_connectors:
        result = result.replace(arrow, " → ")
    # 清理多余空格
    result = re.sub(r'\s*→\s*', ' → ', result)
    result = re.sub(r'(→\s*)+→', '→', result)  # 合并连续箭头
    return result


# ==================== Emoji 清洗 ====================

# 用于清洗 POI 名称首尾的 emoji（改进3: P1b）
EMOJI_STRIP_RE = re.compile(
    r'^[\U0001F300-\U0001F9FF\U00002702-\U000027B0\U0000FE00-\U0000FE0F\U0000200D\U00002600-\U000026FF\U0000231A-\U000023F3\s]+|'
    r'[\U0001F300-\U0001F9FF\U00002702-\U000027B0\U0000FE00-\U0000FE0F\U0000200D\U00002600-\U000026FF\U0000231A-\U000023F3\s]+$'
)


def strip_emoji_from_name(name: str) -> str:
    """
    清洗 POI 名称首尾的 emoji 符号。
    用于返回响应前统一清理 point name。
    """
    if not name:
        return name
    cleaned = EMOJI_STRIP_RE.sub('', name).strip()
    return cleaned if cleaned else name


def clean_poi_name(name: str) -> str:
    """清洗 POI 名称：去除 emoji、括号补充说明、常见景观后缀。"""
    name = strip_emoji_from_name(name).strip()
    # 去除括号内容（中英文括号）
    name = re.sub(r'[（(][^）)]*[）)]', '', name).strip()
    # 小红书常见景点别名/图片小标题，统一到高德可稳定检索的正式名称。
    if re.match(r'^(?:西湖)?曲[苑院]风荷(?:[·•].*)?$', name):
        name = "曲院风荷"
    # 去除常见景观/花卉后缀
    name = re.sub(r'(花海|晚樱|早樱|夜景|日落|夕阳|日出|夜樱|樱花林|花园路|花廊)$', '', name).strip()
    return name


_GENERIC_CITY_NAMES = {
    "北京", "上海", "天津", "重庆", "杭州", "广州", "深圳", "南京", "苏州",
    "成都", "武汉", "西安", "长沙", "郑州", "青岛", "厦门", "宁波", "无锡",
    "济南", "福州", "昆明", "合肥", "南昌", "沈阳", "大连", "哈尔滨",
}


def _is_generic_city_token(name: str, inferred_city: str = "") -> bool:
    """避免把单独城市名误消歧为火车站等具体 POI。"""
    normalized = clean_poi_name(name).removesuffix("市")
    inferred = (inferred_city or "").strip().removesuffix("市")
    return normalized in _GENERIC_CITY_NAMES or bool(inferred and normalized == inferred)


def generate_route_name(points: list, mode: str) -> str:
    """
    生成路线名称，格式：模式 + POI名称，控制在18字以内。
    
    - 2点：步行 A→B
    - 3点：步行 A→B→C
    - 4+点：驾车 A→…→Z(N点)
    """
    mode_label = "步行" if mode == "walk" else "驾车"
    if len(points) <= 3:
        return f"{mode_label} {'→'.join(points)}"
    else:
        return f"{mode_label} {points[0]}→…→{points[-1]}({len(points)}点)"


# ==================== 快速预解析辅助 ====================

# 序号列表模式（每行一个地点）
_NUMBERED_RE = re.compile(r'^\s*(?:\d+[.、)）]\s*|[①②③④⑤⑥⑦⑧⑨⑩❶❷❸❹❺❻❼❽❾❿]\s*)(.+)')

# 时间段前缀（剥离后取地点名）
_TIME_PREFIX_RE = re.compile(
    r'^(?:上午|下午|晚上|早上|中午|傍晚|Day\s*\d+|第[一二三四五六七八九十\d]+天)\s*[：:,，]?\s*'
)

# "路线"前缀（含"路线一/二/三："等编号变体）
_ROUTE_PREFIX_RE = re.compile(
    r'^(?:(?:推荐|游玩|打卡|散步|骑行)?路线[一二三四五六七八九十\d]*[：:]?\s*|'
    r'线路[一二三四五六七八九十\d]*[：:]?\s*|游览顺序[：:]?\s*)'
)

# 小红书正文常在路线前加描述，例如“西湖一日city walk赏花路线：A→B”。
# 只匹配带冒号的路线标签，避免误删地点名或“路线推荐”等普通文案。
_INLINE_ROUTE_PREFIX_RE = re.compile(
    r'(?:路线|线路|游览顺序)[一二三四五六七八九十\d]*[：:]\s*'
)


def _strip_inline_route_prefix(line: str) -> str:
    """箭头路线中剥离最后一个“路线：”及其之前的描述文字。"""
    if '→' not in line:
        return line
    matches = list(_INLINE_ROUTE_PREFIX_RE.finditer(line))
    if not matches:
        return line
    return line[matches[-1].end():].strip()

# 第N站模式
_STATION_RE = re.compile(
    r'^\s*第[一二三四五六七八九十\d]+站[：:\s]*(.+)'
)

# Emoji连接符（用于替换为→再走箭头解析）
_EMOJI_CONNECTOR_RE = re.compile(r'\s*[👉☞▶️🔜]\s*')

# 图片标注（用于从地点名中剥离）
_IMAGE_TAG_RE = re.compile(r'\s*[（(]\s*图\s*[\d/、,]+\s*[）)]')

# 日期/数字范围模式（用于排除短横线误匹配）
_DATE_RANGE_RE = re.compile(r'\d+月?[-–]\d+月?|\d+[-–]\d+(?:km|公里|米|元|块|天|小时)')

# 明显的非地点词（形容词/动词，用于顿号排除）
_NON_PLACE_WORDS = {"好看", "好吃", "好玩", "免费", "收费", "推荐", "必去", "值得", "方便", 
                     "舒服", "干净", "安静", "热闹", "便宜", "漂亮", "很美", "超美", "绝美"}


def _looks_like_place(text: str) -> bool:
    """判断文本是否像地点名：2-15字，不是纯形容词/动词"""
    text = text.strip()
    if not text or len(text) < 1 or len(text) > 20:
        return False
    if text in _NON_PLACE_WORDS:
        return False
    # 排除纯数字/纯标点
    if re.match(r'^[\d\s\W]+$', text) and not re.search(r'[\u4e00-\u9fff]', text):
        return False
    return True


def _try_split_by_separator(text: str, sep: str) -> list:
    """按分隔符切分文本，返回地点列表或空列表"""
    if sep not in text:
        return []
    parts = [p.strip() for p in text.split(sep) if p.strip()]
    # 每段清理时间前缀和序号
    cleaned = []
    for p in parts:
        p = _TIME_PREFIX_RE.sub('', p).strip()
        m = _NUMBERED_RE.match(p)
        if m:
            p = m.group(1).strip()
        if _looks_like_place(p):
            cleaned.append(p)
    return cleaned


def _try_numbered_list(lines: list) -> list:
    """尝试从多行文本中提取序号列表格式的地点"""
    places = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        # 去时间前缀
        line = _TIME_PREFIX_RE.sub('', line).strip()
        m = _NUMBERED_RE.match(line)
        if m:
            place = m.group(1).strip()
            # 地点名后可能跟描述，取第一个标点前的部分
            place = re.split(r'[，,：:（(｜|—]', place)[0].strip()
            if _looks_like_place(place):
                places.append(place)
    return places


def _try_time_segmented(text: str) -> list:
    """尝试提取时间段分隔的地点：上午A，下午B，晚上C"""
    if not _TIME_PREFIX_RE.search(text):
        return []
    # 按逗号/句号/换行切分
    segments = re.split(r'[，,。\n]+', text)
    places = []
    for seg in segments:
        seg = seg.strip()
        m = _TIME_PREFIX_RE.match(seg)
        if m:
            rest = seg[m.end():].strip()
            # 取第一个标点前的部分作为地点名
            place = re.split(r'[，,：:（(｜|—\s]', rest)[0].strip()
            if _looks_like_place(place) and len(place) >= 2:
                places.append(place)
    return places


def _try_station_list(lines: list) -> list:
    """提取"第N站"格式的地点列表"""
    places = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        m = _STATION_RE.match(line)
        if m:
            place = m.group(1).strip()
            place = _IMAGE_TAG_RE.sub('', place).strip()
            place = re.split(r'[，,：:（(｜|—]', place)[0].strip()
            place = clean_poi_name(place)
            if _looks_like_place(place) and len(place) >= 2:
                places.append(place)
    return places


def _try_pin_route(lines: list) -> list:
    """提取小红书常见的“📍地点-📍地点”路线行。"""
    for line in lines:
        if line.count("📍") < 2:
            continue

        route_line = line[line.find("📍"):]
        raw_parts = re.split(r'\s*(?:-|–|—|→)\s*', route_line)
        places = []
        for part in raw_parts:
            place = part.lstrip("📍").strip()
            # 保留括号中的入口/出口信息，避免“苏堤北口”和“苏堤”被合并。
            place = re.sub(r'[（(]([^）)]+)[）)]', r'\1', place)
            place = strip_emoji_from_name(place).strip()
            if _looks_like_place(place):
                places.append(place)

        if len(places) >= 2:
            return places

    return []


# ==================== 快速预解析（跳过AI） ====================

def try_fast_parse(text: str) -> dict:
    """
    快速解析半结构化文本，避免调用AI。

    支持格式（按优先级）：
    1. A → B → C（箭头，已由 normalize_connectors 统一）
    1b. A 👉 B 👉 C（emoji 连接符）
    2. A / B / C（斜杠分隔）
    3. A - B - C（短横线分隔，排除日期范围）
    4. A、B、C（顿号分隔，排除纯形容词）
    5. 序号列表（1. A  2. B  3. C / ❶ A ❷ B）
    5b. 第N站列表（第一站：A  第二站：B）
    6. 时间段（上午A，下午B，晚上C）
    7. "路线："前缀 + 上述任意分隔符

    返回 route_info 结构；无法识别时返回 None
    """
    if not text or not text.strip():
        return None
    
    working_text = text.strip()
    
    # 剥离"路线："类前缀
    m = _ROUTE_PREFIX_RE.match(working_text)
    if m:
        working_text = working_text[m.end():].strip()
    
    lines = [l.strip() for l in working_text.splitlines() if l.strip()]
    
    def _make_result(parts: list) -> dict:
        if len(parts) >= 2:
            return {
                "content_type": "single_route",
                "city": "",
                "routes": [{"name": "", "description": "", "points": parts}]
            }
        return None
    
    # 预处理：emoji 连接符替换为 →
    working_text = _EMOJI_CONNECTOR_RE.sub('→', working_text)
    # 对每行剥离"路线："类前缀
    processed_lines = []
    for l in working_text.splitlines():
        l = l.strip()
        if not l:
            continue
        l = _strip_inline_route_prefix(l)
        pm = _ROUTE_PREFIX_RE.match(l)
        if pm:
            l = l[pm.end():].strip()
        if l:
            processed_lines.append(l)
    lines = processed_lines

    # 格式0: 小红书正文中的 📍A-📍B-📍C 路线行
    pin_places = _try_pin_route(lines)
    result = _make_result(pin_places)
    if result:
        return result

    def _split_arrow_line(line):
        parts = [clean_poi_name(_IMAGE_TAG_RE.sub('', p).strip())
                 for p in line.split('→') if p.strip()]
        return [p for p in parts if p and _looks_like_place(p)]

    # --- 单行模式 ---
    if len(lines) == 1:
        line = lines[0]

        # 格式1: 箭头 A → B → C（含 emoji 连接符已转为 →）
        if '→' in line:
            parts = _split_arrow_line(line)
            if len(parts) >= 2:
                result = _make_result(parts)
                if result:
                    return result

        # 格式2: 斜杠 A / B / C
        if '/' in line:
            parts = _try_split_by_separator(line, '/')
            result = _make_result(parts)
            if result:
                return result
        
        # 格式3: 短横线 A - B - C（排除日期范围）
        if '-' in line and not _DATE_RANGE_RE.search(line):
            parts = _try_split_by_separator(line, '-')
            result = _make_result(parts)
            if result:
                return result
        
        # 格式4: 顿号 A、B、C
        if '、' in line:
            parts = _try_split_by_separator(line, '、')
            # 顿号需要更严格：至少2个像地点名的词
            if len(parts) >= 2 and sum(1 for p in parts if len(p) >= 2) >= 2:
                result = _make_result(parts)
                if result:
                    return result
        
        # 格式6: 时间段 上午A，下午B
        time_places = _try_time_segmented(line)
        result = _make_result(time_places)
        if result:
            return result
        
        return None
    
    # --- 多行模式 ---

    # 格式1: 多行箭头（每行一条路线）
    arrow_routes = []
    for line in lines:
        if '→' in line:
            parts = _split_arrow_line(line)
            if len(parts) >= 2:
                arrow_routes.append({"name": "", "description": "", "points": parts})
    if arrow_routes:
        return {
            "content_type": "single_route" if len(arrow_routes) == 1 else "multi_route",
            "city": "",
            "routes": arrow_routes
        }

    # 格式5b: 第N站列表
    station_places = _try_station_list(lines)
    if len(station_places) >= 2:
        result = _make_result(station_places)
        if result:
            return result

    # 格式5: 序号列表（每行一个地点）
    numbered_places = _try_numbered_list(lines)
    if len(numbered_places) >= 3:
        result = _make_result(numbered_places)
        if result:
            return result

    # 格式6: 多行时间段
    time_places = _try_time_segmented(working_text)
    result = _make_result(time_places)
    if result:
        return result

    return None


# ==================== 小红书链接解析 ====================

def extract_xhs_link(text: str) -> str:
    """从文本中提取小红书链接（支持移动端和PC端格式）"""
    
    # 1. 匹配移动端短链：新分享为 .cn，旧分享仍可能是 .com。
    pattern = r'https?://(?:www\.)?xhslink\.(?:com|cn)/[^\s<>"，。]+'
    match = re.search(pattern, text)
    if match:
        return match.group().rstrip('.,;:!?)]}\'，。；：！？）】')
    
    # 2. 匹配 xiaohongshu.com 长链（PC端，带参数）
    # 示例: https://www.xiaohongshu.com/discovery/item/69a7f99b000000000d00a037?source=webshare&...
    pattern = r'https?://(?:www\.)?xiaohongshu\.com/(?:discovery/item|explore)/[a-zA-Z0-9]+(?:\?[^\s]*)?' 
    match = re.search(pattern, text)
    if match:
        # 清理URL，只保留核心部分
        url = match.group()
        # 移除尾部的特殊字符
        url = url.rstrip('?&')
        return url
    
    return None


def _parse_xhs_response(response) -> dict:
    """严格校验小红书响应，登录错误页和空正文都不算成功。"""
    final_url = response.url
    decoded_url = urllib.parse.unquote(final_url)
    error_match = re.search(r"error_code=(\d+)", decoded_url)
    upstream_code = error_match.group(1) if error_match else None

    if "/website-login/error" in decoded_url or upstream_code:
        return {
            "success": False,
            "error": "小红书返回登录/风控错误页",
            "upstream_code": upstream_code or "unknown",
            "url": final_url,
        }
    if response.status_code >= 400:
        return {
            "success": False,
            "error": f"小红书 HTTP {response.status_code}",
            "upstream_code": str(response.status_code),
            "url": final_url,
        }

    html = response.text
    soup = BeautifulSoup(html, "html.parser")
    title = ""
    content = ""

    invalid_titles = {"小红书", "搜索小红书"}
    for candidate in re.findall(r'"title":"(.*?)"(?=,")', html):
        candidate = candidate.strip()
        if candidate and candidate not in invalid_titles and len(candidate) > 2:
            title = candidate
            break

    for candidate in re.findall(r'"desc":"(.*?)"(?=,")', html):
        candidate = candidate.strip()
        if len(candidate) > len(content):
            content = candidate

    if content:
        content = (
            content.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\u002F", "/")
            .replace("\\u0026", "&")
        )
    else:
        desc_tag = soup.find("meta", property="og:description") or soup.find(
            "meta", attrs={"name": "description"}
        )
        if desc_tag:
            content = desc_tag.get("content", "").strip()

    if not title:
        og_title = soup.find("meta", property="og:title")
        if og_title:
            candidate = og_title.get("content", "").strip()
            if candidate not in invalid_titles:
                title = candidate

    full_text = f"{title}\n\n{content}" if title else content
    if len(full_text.strip()) < 20:
        return {
            "success": False,
            "error": "小红书正文为空或不完整",
            "upstream_code": None,
            "url": final_url,
        }

    return {
        "success": True,
        "title": title,
        "content": content,
        "full_text": full_text,
        "url": final_url,
    }


def fetch_xhs_content(url: str) -> dict:
    """使用两种现代浏览器标识抓取；最多两次，不依赖登录 Cookie。"""
    strategies = (
        ("mobile", XHS_MOBILE_USER_AGENT),
        ("desktop", XHS_DESKTOP_USER_AGENT),
    )
    last_failure = None

    for attempt, (strategy, user_agent) in enumerate(strategies, start=1):
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        try:
            session = requests.Session()
            response = session.get(
                url,
                headers=headers,
                timeout=XHS_FETCH_TIMEOUT,
                allow_redirects=True,
            )
            result = _parse_xhs_response(response)
        except requests.RequestException as exc:
            result = {
                "success": False,
                "error": str(exc),
                "upstream_code": None,
            }

        result["attempts"] = attempt
        result["strategy"] = strategy
        if result.get("success"):
            stat_key = "primary_success" if attempt == 1 else "fallback_success"
            _xhs_fetch_stats[stat_key] += 1
            return result

        if result.get("upstream_code") == "300011":
            _xhs_fetch_stats["error_300011"] += 1
        last_failure = result

    _xhs_fetch_stats["failures"] += 1
    return {
        "success": False,
        "error": (last_failure or {}).get("error", "小红书正文抓取失败"),
        "error_code": "xhs_fetch_failed",
        "upstream_code": (last_failure or {}).get("upstream_code"),
        "retryable": True,
        "attempts": len(strategies),
    }


def _normalize_client_source_text(source_text: str, source_url: str) -> dict:
    """将 iPhone 传来的网页 HTML 提取为正文；纯文本则直接使用。"""
    stripped = source_text.strip()
    looks_like_html = (
        stripped.startswith("<!DOCTYPE")
        or stripped.startswith("<html")
        or '"desc":"' in stripped
        or "<meta" in stripped[:5000]
    )
    if not looks_like_html:
        if len(stripped) < 20:
            return {"success": False, "error": "手机端正文为空或不完整"}
        return {
            "success": True,
            "title": "",
            "content": stripped,
            "full_text": stripped,
            "url": source_url,
            "strategy": "client_source_text",
        }

    response_like = type(
        "ClientSourceResponse",
        (),
        {"url": source_url, "status_code": 200, "text": stripped},
    )()
    result = _parse_xhs_response(response_like)
    result["strategy"] = "client_source_html"
    return result

# ==================== AI 提取路线 ====================

def extract_route_with_ai(text: str) -> dict:
    """
    使用 DeepSeek 分析文本，提取路线信息
    v3.0: 支持多路线识别
    返回：{
        "content_type": "single_route" | "multi_route" | "poi_list" | "no_route",
        "city": "城市",
        "routes": [{"name": "路线名", "description": "描述", "points": ["地点1", ...]}, ...]
    }
    """
    
    prompt = f"""请分析以下小红书帖子内容，识别其中的游览路线。

帖子内容：
{text}

请以JSON格式返回，包含以下字段：

1. content_type: 内容类型
   - "single_route": 只有一条路线
   - "multi_route": 包含多条独立路线（如"8条赏花路线推荐"）
   - "poi_list": 景点/POI推荐列表，没有明确的游览顺序（如"20个赏樱地推荐"）
   - "no_route": 完全没有路线信息，且无法识别出任何地点名称
     重要：如果文中能识别出 3 个及以上地点/景点/店铺名称，即使没有明确路线顺序，也应返回 poi_list 而非 no_route。宁可多返回地点，也不返回空的 no_route。

2. city: 字符串，推测的城市名称（如无法判断则为空）

3. routes: 路线数组，每条路线包含：
   - name: 路线名称（如"西湖西线赏樱路线"，如无明确名称则自动生成）
   - description: 简短描述（如"3月中-4月初，郁金香樱花"，可为空）
   - points: 地点数组，按游览顺序排列（只要地点名，不要附加描述）
   - points 中只返回可被高德地图搜索到的标准地名
   - 去掉花卉品种（晚樱/木绣球）、拍摄日期、景观描述（花海/夜景）等后缀
   - 去掉括号内的补充说明，如"（拍摄于3·17）""（双峰馆区）"
   - 示例："乌龟潭晚樱" → "乌龟潭"，"九堡大桥木绣球花海" → "九堡大桥"

## 复合地点名称解析（重要）：
   - 当遇到"XX地铁站Y口+地点"的结构时，地点本身是主体，地铁站只是到达方式
   - 示例："龙翔桥地铁站C口音乐喷泉处" → 提取"西湖音乐喷泉"，不要单独提取"龙翔桥地铁站"
   - 示例："凤起路地铁站B口+断桥残雪" → 提取"断桥残雪"
   - 判断原则：如果去掉地铁站部分，剩下的仍是完整地点名称，则只保留地点

## 环线路线识别（重要）：
   - 如果路线描述中出现"回到起点"、"返回XX"、"终点即起点"等字样，说明是环线
   - 环线points数组的第一个点和最后一个点应该是同一个地点
   - 示例："音乐喷泉→...→南山路→音乐喷泉（起点）" → points应为["西湖音乐喷泉", ..., "南山路", "西湖音乐喷泉"]

**重要：对于 poi_list 类型，也必须提取所有提到的地点名称**
   - 在 routes 中返回一个元素，name 可以是"推荐地点"或根据内容生成
   - points 数组包含所有提到的 POI/景点/店铺名称（无需排序）
   - 相同地点只保留一次（如"乌龟潭"出现多次，points 中只出现一个）

## Emoji语义识别规则（重要）：

1. **连接符号**（表示"从A到B"的顺序关系）：
   - 箭头类：→ ➡️ ➜ ➔ 👉 ☞ ▶️ ⇒
   - 当emoji出现在"地点A + emoji + 地点B"的结构中，该emoji是连接符
   - 示例："玛瑙寺 👉 苏堤北口 👉 曲院风荷" 中的👉是连接符

2. **序号标记**（表示第N条路线或第N个步骤）：
   - 数字圈：① ② ③ ❶ ❷ 1️⃣ 2️⃣
   - 通常出现在行首，后面跟路线名或地点
   - 示例："①西湖南线 ②龙井茶园线" 中①②是路线序号

3. **位置标记**（强调某个地点）：
   - 定位类：📍 📌 🎯
   - 当出现在地点名前面时，是位置强调，不是连接符
   - 示例："📍玛瑙寺" 中📍是位置标记，玛瑙寺是地点

4. **修饰装饰**（不影响路线结构）：
   - 花草类：🌸 🌺 🌹 用于装饰标题
   - 交通类：🚗 🚌 🚶 表示交通方式说明
   - 时间类：📅 ⏰ 表示时间信息
   - 这些不是连接符，忽略它们对路线顺序的影响

## 判断技巧：
- 连接符的特征：两侧都是地点名
- 序号的特征：在行首或段落开头
- 修饰的特征：单独出现或与非地点词搭配

只返回JSON，不要其他内容。"""

    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,  # 低温度，更确定性的输出
    }
    
    try:
        response = requests.post(DEEPSEEK_API_URL, headers=headers, json=payload, timeout=60)
        response.raise_for_status()
        
        result = response.json()
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        
        # 解析 JSON
        # 尝试提取 JSON 部分（处理可能的 markdown 代码块）
        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            route_info = json.loads(json_match.group())
            return route_info
        else:
            return {"is_route": False, "locations": [], "city": "", "error": "无法解析AI返回"}
            
    except Exception as e:
        return {"is_route": False, "locations": [], "city": "", "error": str(e)}


# ==================== 高德地理编码 ====================
# 已迁移至 poi_disambiguate.py，此处通过 import 引入 geocode / batch_geocode


# ==================== 构建高德URL ====================

def build_amap_url(locations: list, mode: int = 2) -> str:
    """
    构建高德地图URL Scheme
    locations: [{"name": str, "lat": float, "lon": float}, ...]
    mode: 0=驾车, 1=公交, 2=步行, 3=骑行

    环线处理：起点=终点时高德URL终点会显示为空，用倒数第二个点作为终点。
    """
    if len(locations) < 2:
        return None

    origin = locations[0]

    def _is_same_point(a, b):
        try:
            return abs(a.get('lat', 0) - b.get('lat', 0)) < 0.002 and abs(a.get('lon', 0) - b.get('lon', 0)) < 0.002
        except:
            return False

    if _is_same_point(origin, locations[-1]) and len(locations) >= 3:
        destination = locations[-2]
        waypoints = locations[1:-2]
    else:
        destination = locations[-1]
        waypoints = locations[1:-1]
    
    url = "iosamap://path?sourceApplication=RouteSnap"
    
    # 起点
    url += f"&slat={origin['lat']}&slon={origin['lon']}"
    url += f"&sname={urllib.parse.quote(origin['name'])}"
    
    # 终点
    url += f"&dlat={destination['lat']}&dlon={destination['lon']}"
    url += f"&dname={urllib.parse.quote(destination['name'])}"
    
    # 途经点
    if waypoints:
        url += f"&vian={len(waypoints)}"
        url += f"&vialons={'|'.join(str(w['lon']) for w in waypoints)}"
        url += f"&vialats={'|'.join(str(w['lat']) for w in waypoints)}"
        url += f"&vianames={'|'.join(urllib.parse.quote(w['name']) for w in waypoints)}"
    
    url += f"&dev=0&t={mode}"
    
    return url


# ==================== API 接口 ====================

@app.route("/", methods=["GET"])
def index():
    """健康检查"""
    return jsonify({
        "service": "RouteSnap API",
        "version": APP_VERSION,
        "status": "running",
        "usage": "POST /parse with Authorization: Bearer <token>",
        **configuration_status(),
    })


@app.route("/parse", methods=["POST"])
def parse_route():
    """
    解析帖子内容或链接，返回高德导航URL
    v3.0: 支持多路线识别与选择
    
    请求体：
    {
        "text": "帖子内容或包含小红书链接的文本",
        "mode": 2,  // 可选，导航模式：0驾车/1公交/2步行/3骑行
        "source_text": "手机端取得的正文（可选，仅抓取失败时使用）",
        "source_url": "对应的小红书链接（可选）"
    }
    
    返回（多路线）：
    {
        "success": true,
        "content_type": "multi_route",
        "routes": [
            {
                "index": 1,
                "name": "莫奈花园太子湾线",
                "description": "3月中-4月初",
                "points": ["西子宾馆", "净慈寺", "太子湾", "九曜山"],
                "locations": [{lat, lon, name}, ...],
                "amap_url": "iosamap://..."
            },
            ...
        ]
    }
    """
    try:
        data = request.get_json(silent=True) or {}
        text = str(data.get("text") or "")
        source_text = str(data.get("source_text") or "").strip()
        source_url_value = data.get("source_url") or ""
        if isinstance(source_url_value, list):
            source_url_value = source_url_value[0] if source_url_value else ""
        source_url = str(source_url_value).strip()
        try:
            mode = int(data.get("mode", 2))
        except (TypeError, ValueError):
            mode = 2
        if mode not in (0, 1, 2, 3):
            mode = 2
        city_hint = data.get("city", "")  # 用户手选城市（可选）
        
        if not text and not source_text:
            return jsonify({"success": False, "error": "缺少text或source_text参数"}), 400
        if len(source_text) > 2000000:
            return jsonify({"success": False, "error": "source_text超过2000000字符"}), 413
        
        # 结构化请求日志
        _req_start = time.monotonic()
        req_log = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "input_length": len(text or source_text),
            "has_xhs_link": False,
            "parse_path": None,
            "point_count": 0,
            "cache_hits": {"route": False, "hot_poi": 0, "poi_cache": 0, "api_call": 0},
            "disambiguation": {"auto_accept": 0, "needs_confirm": 0, "failed": 0},
            "total_ms": 0,
        }
        
        # 改进4: P2b - Debug 性能追踪
        _timing = {
            "ai_start": 0,
            "ai_end": 0,
            "geocode_start": 0,
            "geocode_end": 0,
        }
        
        def _log_and_return(response_data, status_code=200):
            """统一日志打印和返回"""
            req_log["total_ms"] = int((time.monotonic() - _req_start) * 1000)
            print(f"[REQ] {json.dumps(req_log, ensure_ascii=False)}")
            if app.debug:
                response_data["_debug"] = req_log
            if status_code == 200:
                return jsonify(response_data)
            return jsonify(response_data), status_code
        
        # 检测是否包含小红书链接；source_url 用于手机端最终降级。
        xhs_link = extract_xhs_link(source_url) or extract_xhs_link(text)
        content_to_analyze = source_text or text
        xhs_info = None
        route_key = None

        if xhs_link:
            req_log["has_xhs_link"] = True
            # 小红书缓存必须在网络抓取之前检查，服务重启后仍可直接命中。
            route_key = _route_source_cache_key(xhs_link, mode, city_hint)
            cached = _get_route_cache(route_key)
            if cached is not None:
                print(f"[路线缓存] 小红书链接命中: {route_key}")
                req_log["cache_hits"]["route"] = True
                req_log["parse_path"] = "cache"
                return _log_and_return(cached)

            if source_text:
                # iPhone 最终降级已提供正文，服务端不再重复抓取。
                xhs_info = _normalize_client_source_text(source_text, xhs_link)
                if not xhs_info.get("success"):
                    req_log["parse_path"] = "client_source_invalid"
                    return _log_and_return({
                        "success": False,
                        "error": "手机端未取得有效的小红书正文",
                        "error_code": "xhs_fetch_failed",
                        "upstream_code": xhs_info.get("upstream_code"),
                        "retryable": True,
                    })
                content_to_analyze = xhs_info["full_text"]
            else:
                xhs_info = fetch_xhs_content(xhs_link)
                if xhs_info.get("success") and xhs_info.get("full_text"):
                    content_to_analyze = xhs_info["full_text"]
                else:
                    _xhs_fetch_stats["blocked_title_fallbacks"] += 1
                    req_log["parse_path"] = "xhs_fetch_failed"
                    return _log_and_return({
                        "success": False,
                        "error": "小红书正文抓取失败，未生成猜测路线",
                        "error_code": "xhs_fetch_failed",
                        "upstream_code": xhs_info.get("upstream_code"),
                        "retryable": True,
                    })

        if route_key is None:
            route_key = _route_cache_key(content_to_analyze, mode, city_hint)
            cached = _get_route_cache(route_key)
            if cached is not None:
                print(f"[路线缓存] 命中: {route_key}")
                req_log["cache_hits"]["route"] = True
                req_log["parse_path"] = "cache"
                return _log_and_return(cached)
        
        # 1. 预处理：统一箭头符号
        normalized_content = normalize_connectors(content_to_analyze)
        
        # 2. 优先解析正文中明确的路线格式，避免不必要的 AI 依赖。
        fast_result = try_fast_parse(normalized_content)
        if fast_result is not None:
            route_info = fast_result
            req_log["parse_path"] = "fast_parse"
        else:
            # 3. 非结构化正文再交给 AI 分析。
            _timing["ai_start"] = time.monotonic()
            route_info = extract_route_with_ai(normalized_content[:2000])
            _timing["ai_end"] = time.monotonic()
            req_log["parse_path"] = "ai" if route_info.get("is_route") else "ai_failed"
        
        # === Emoji 自学习 ===
        try:
            process_text_for_learning(
                text=content_to_analyze,
                route_info=route_info,
                library_path=EMOJI_LIBRARY_PATH
            )
        except Exception as e:
            print(f"[Emoji学习] 处理失败（不影响主流程）: {e}")
        
        content_type = route_info.get("content_type", "no_route")
        city = route_info.get("city", "")
        ai_routes = route_info.get("routes", [])
        
        # ===== 二次 AI 调用：no_route 容错机制 =====
        # 当一次解析返回 no_route 时，尝试专注提取地名
        if content_type == "no_route" or not ai_routes:
            print("[二次AI调用] 一次解析返回 no_route，尝试提取地名...")
            fallback_prompt = f"""请从以下文本中提取所有出现的地点、景点、店铺、公园、餐厅等地名。
不需要判断是否有路线，只需要列出所有提到的地名。
每个地名只返回可被高德地图搜索到的标准名称，去掉修饰词。

文本：
{normalized_content[:2000]}

以JSON格式返回：
"places": ["地名1", "地名2", ...]
只返回JSON，不要其他内容。"""

            try:
                fallback_response = requests.post(
                    DEEPSEEK_API_URL,
                    headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "model": DEEPSEEK_MODEL,
                        "messages": [{"role": "user", "content": fallback_prompt}],
                        "temperature": 0.1,
                    },
                    timeout=30,
                )
                fallback_response.raise_for_status()
                fallback_text = fallback_response.json()["choices"][0]["message"]["content"]
                # 清理 markdown 代码块
                fallback_text = fallback_text.strip()
                if fallback_text.startswith("```"):
                    fallback_text = fallback_text.split("\n", 1)[1] if "\n" in fallback_text else fallback_text[3:]
                if fallback_text.endswith("```"):
                    fallback_text = fallback_text[:-3]
                fallback_text = fallback_text.strip()
                
                fallback_result = json.loads(fallback_text)
                places = fallback_result.get("places", [])
                print(f"[二次AI调用] 提取到 {len(places)} 个地名: {places[:5]}...")
                
                if len(places) >= 2:
                    # 有地名，转为 poi_list 流程
                    content_type = "poi_list"
                    ai_routes = [{"name": "AI提取的地点", "points": places}]
                    print(f"[二次AI调用] 成功，转为 poi_list 处理，共 {len(places)} 个地名")
                    # 不 return，让代码继续走到下面的 poi_list 处理分支
                else:
                    print("[二次AI调用] 仍未提取到足够地名，返回失败")
                    return _log_and_return({
                        "success": False,
                        "content_type": "no_route",
                        "message": "未识别到有效路线或地点",
                        "analyzed_text": normalized_content[:200] + "..." if len(normalized_content) > 200 else normalized_content,
                        "xhs_info": xhs_info,
                        "raw": route_info,
                    })
            except Exception as e:
                print(f"[二次AI调用] 失败: {e}")
                return _log_and_return({
                    "success": False,
                    "content_type": "no_route",
                    "message": "未识别到有效路线",
                    "analyzed_text": normalized_content[:200] + "..." if len(normalized_content) > 200 else normalized_content,
                    "xhs_info": xhs_info,
                    "raw": route_info,
                })
        
        # 改进2: P1a — POI 推荐列表增值
        if content_type == "poi_list":
            # 从 AI 返回结果中获取 POI 名称列表
            poi_names = []
            for route in ai_routes:
                poi_names.extend(route.get("points", []))
            
            # 文本层清洗+去重（保持顺序）
            seen = set()
            unique_names = []
            for name in poi_names:
                cleaned = clean_poi_name(name)
                if (
                    cleaned
                    and not _is_generic_city_token(cleaned, city_hint or city)
                    and cleaned not in seen
                ):
                    seen.add(cleaned)
                    unique_names.append(cleaned)
            poi_names = unique_names
            
            if poi_names:
                # 对 POI 进行地理编码
                requested_mode = {0: "drive", 1: "transit", 2: "walk", 3: "bike"}.get(mode, "walk")
                _timing["geocode_start"] = time.monotonic()
                disambiguation = disambiguate_route(
                    poi_names, text=content_to_analyze, city=city_hint or city, mode=requested_mode
                )
                _timing["geocode_end"] = time.monotonic()
                
                # 构建 POI 列表
                geocoded_pois = []
                for dr in disambiguation:
                    poi = dr.get("selected_poi")
                    status = dr.get("status", "")
                    if poi and status != "failed":
                        # 改进3: 清洗 POI 名称
                        cleaned_name = strip_emoji_from_name(poi.get("name", ""))
                        geocoded_pois.append({
                            "name": cleaned_name,
                            "location": f"{poi['lon']},{poi['lat']}",
                            "confidence": dr.get("confidence", 0),
                        })
                
                # 坐标层去重：距离 < 200m 的 POI 视为同一地点，保留名称较短的
                deduped_pois = []
                for poi in geocoded_pois:
                    is_dup = False
                    for existing in deduped_pois:
                        dist = haversine(poi["location"], existing["location"])
                        if dist < 0.2:  # 200m
                            # 保留名称较短的
                            if len(poi["name"]) < len(existing["name"]):
                                deduped_pois.remove(existing)
                                deduped_pois.append(poi)
                            is_dup = True
                            break
                    if not is_dup:
                        deduped_pois.append(poi)
                geocoded_pois = deduped_pois
                
                req_log["point_count"] = len(poi_names)
                
                # ===== 两层聚类 + 路线生成 =====
                if geocoded_pois:
                    clusters = cluster_pois_by_distance(geocoded_pois)
                    # clusters 格式: [{"mode": "walk"/"drive", "pois": [...]}]
                    
                    routes = []
                    total_pois = sum(len(c["pois"]) for c in clusters)
                    for cluster in clusters:
                        sorted_pois = cluster["pois"]  # 已在 cluster 函数内排好序
                        
                        point_names = [p["name"] for p in sorted_pois]
                        route_name = generate_route_name(point_names, requested_mode)
                        
                        # 构建坐标列表用于 build_amap_url
                        locations = []
                        for p in sorted_pois:
                            parts = p["location"].split(",")
                            lon, lat = float(parts[0]), float(parts[1])
                            locations.append({"name": p["name"], "lat": lat, "lon": lon})
                        
                        if len(locations) >= 2:
                            # 聚类只负责分组和排序，最终导航模式必须尊重请求 mode。
                            amap_url = build_amap_url(locations, mode)
                        else:
                            # 单点（理论上不会到这里，因为 <2 的簇已被丢弃）
                            loc = locations[0]
                            from urllib.parse import quote
                            amap_url = f"iosamap://navi?sourceApplication=RouteSnap&poiname={quote(loc['name'])}&lat={loc['lat']}&lon={loc['lon']}&dev=0&style=2"
                        
                        routes.append({
                            "name": route_name,
                            "mode": requested_mode,
                            "points": point_names,
                            "amap_url": amap_url,
                            "point_count": len(sorted_pois),
                        })
                    
                    if routes:
                        response_data = {
                            "success": True,
                            "content_type": "poi_list",
                            "route_count": len(routes),
                            "routes": routes,
                            "message": f"识别到 {total_pois} 个地点，生成 {len(routes)} 条路线",
                        }
                    else:
                        response_data = {
                            "success": True,
                            "content_type": "poi_list",
                            "route_count": 0,
                            "routes": [],
                            "message": "地点较为分散，建议在高德地图逐个搜索",
                        }
                else:
                    response_data = {
                        "success": False,
                        "content_type": "poi_list",
                        "message": "识别到推荐地点但地理编码全部失败",
                    }

                _set_route_cache(route_key, response_data)
                return _log_and_return(response_data)
            else:
                return _log_and_return({
                    "success": False,
                    "content_type": content_type,
                    "message": "未识别到有效的推荐地点",
                    "raw": route_info
                })
        
        # 注意：no_route 的处理已移至 poi_list 分支之前（二次 AI 调用容错机制）
        # 如果到达这里且仍是 no_route，说明二次调用也失败了（已在上面 return）
        
        # 2. 对每条路线进行地理编码和URL构建
        processed_routes = []
        for idx, route in enumerate(ai_routes):
            route_name = route.get("name", f"路线{idx+1}")
            route_desc = route.get("description", "")
            points = [
                point for point in route.get("points", [])
                if not _is_generic_city_token(point, city_hint or city)
            ]
            
            if len(points) < 2:
                continue
            
            # POI 消歧（候选召回 + 多维打分 + 置信度分层）
            mode_str = {0: "drive", 1: "transit", 2: "walk", 3: "bike"}.get(mode, "walk")
            _timing["geocode_start"] = time.monotonic()
            disambiguation = disambiguate_route(
                points, text=content_to_analyze, city=city_hint or city, mode=mode_str
            )
            _timing["geocode_end"] = time.monotonic()
            
            # ===== 离群点检测（所有模式均执行，移除超过5km的离群点）=====
            if True:  # 所有模式都执行离群点检测
                # 计算每个点到最近邻的距离
                valid_results = [r for r in disambiguation if r.get("status") != "failed" and r.get("selected_poi")]
                if len(valid_results) > 2:
                    filtered = []
                    for i, r in enumerate(valid_results):
                        poi = r["selected_poi"]
                        loc_i = f"{poi['lon']},{poi['lat']}"
                        min_dist = float('inf')
                        for j, r2 in enumerate(valid_results):
                            if i == j:
                                continue
                            poi2 = r2["selected_poi"]
                            loc_j = f"{poi2['lon']},{poi2['lat']}"
                            d = haversine(loc_i, loc_j)
                            if d < min_dist:
                                min_dist = d
                        if min_dist <= 5:  # 5km 以内有邻居
                            filtered.append(r)
                        # else: 离群点，静默丢弃
                    
                    if len(filtered) >= 2:
                        disambiguation = filtered  # 用过滤后的结果替换
            
            # 改进1: P0 — 检查公园内路线
            if disambiguation and disambiguation[0].get("park_internal") is True:
                park_info = disambiguation[0]
                # 清洗名称
                park_name = strip_emoji_from_name(park_info.get("name", ""))
                park_location = park_info.get("location", "")
                internal_points = park_info.get("internal_points", [])
                
                # 解析坐标
                try:
                    lon, lat = park_location.split(",")
                    park_lat, park_lon = float(lat), float(lon)
                except:
                    park_lat, park_lon = 0, 0
                
                # 生成单点导航 URL
                single_location = [{"name": park_name, "lat": park_lat, "lon": park_lon}]
                # 单点导航需要至少2个点，起点设为当前位置（空），终点为公园
                # 使用 iosamap://navi 格式进行单点导航
                amap_url = f"iosamap://navi?sourceApplication=RouteSnap&poiname={urllib.parse.quote(park_name)}&lat={park_lat}&lon={park_lon}&dev=0&style=2"
                
                processed_routes.append({
                    "index": idx + 1,
                    "name": route_name,
                    "mode": mode_str,
                    "description": route_desc,
                    "points": [park_name],  # 只包含父级 POI
                    "locations": [{
                        "name": park_name,
                        "lat": park_lat,
                        "lon": park_lon,
                    }],
                    "internal_points": [strip_emoji_from_name(p) for p in internal_points],  # 原始子景点列表
                    "content_type": "park_internal",
                    "amap_url": amap_url,
                })
                continue  # 跳过后续处理，直接处理下一条路线
            
            # 从消歧结果中提取 locations 和 failed
            locations = []
            failed = []
            for dr in disambiguation:
                poi = dr.get("selected_poi")
                status = dr.get("status", "")
                if poi and status != "failed":
                    locations.append(poi)
                else:
                    failed.append(dr.get("query", dr.get("raw_query", "")))
                # 统计消歧结果
                if status == "auto_accept":
                    req_log["disambiguation"]["auto_accept"] += 1
                elif status == "needs_confirm":
                    req_log["disambiguation"]["needs_confirm"] += 1
                elif status == "failed":
                    req_log["disambiguation"]["failed"] += 1
            req_log["point_count"] = len(points)
            
            # 至少需要2个有效地点
            if len(locations) >= 2:
                # ===== 环线检测 =====
                is_loop = False
                first = locations[0]
                last = locations[-1]
                first_name = first.get("name", "").replace("杭州西湖风景名胜区-", "").replace("景区", "")
                last_name = last.get("name", "").replace("杭州西湖风景名胜区-", "").replace("景区", "")
                name_match = first_name == last_name or first_name in last_name or last_name in first_name
                try:
                    from math import radians, sin, cos, sqrt, atan2
                    lat1, lon1 = radians(first["lat"]), radians(first["lon"])
                    lat2, lon2 = radians(last["lat"]), radians(last["lon"])
                    dlat, dlon = lat2 - lat1, lon2 - lon1
                    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
                    dist_km = 6371 * 2 * atan2(sqrt(a), sqrt(1-a))
                    dist_match = dist_km < 0.1
                except:
                    dist_match = False
                is_loop = name_match or dist_match
                if is_loop:
                    route_desc = (route_desc + " [环线]").strip() if route_desc else "[环线]"

                amap_url = build_amap_url(locations, mode)
                # 改进3: P1b — 清洗 points 名称
                cleaned_points = [strip_emoji_from_name(p) for p in points]
                processed_routes.append({
                    "index": idx + 1,
                    "name": route_name,
                    "mode": mode_str,
                    "description": route_desc,
                    "points": cleaned_points,
                    "locations": locations,
                    "failed": failed if failed else None,
                    "amap_url": amap_url,
                    "is_loop": is_loop,
                    "disambiguation": [  # 消歧详情
                        {
                            "query": dr["query"],
                            "confidence": dr.get("confidence", 0),
                            "status": dr["status"],
                            "candidates": dr.get("candidates", []),
                        }
                        for dr in disambiguation
                    ],
                })
        
        if not processed_routes:
            return _log_and_return({
                "success": False,
                "content_type": content_type,
                "message": "所有路线地理编码失败",
                "raw": route_info
            })
        
        # 3. 构建歧义点交互数据
        ambiguous_points = []
        for idx, route in enumerate(processed_routes):
            disambiguation_results = route.get("disambiguation", [])
            for pt_idx, d in enumerate(disambiguation_results):
                if d.get("status") == "needs_confirm" and d.get("candidates"):
                    # 构建 options 文本（供 Shortcuts Choose from Menu 使用）
                    options = []
                    option_details = []
                    for cand in d["candidates"][:3]:
                        label = cand.get("name", "")
                        dist_info = f"（{cand.get('district', cand.get('city', ''))}）"
                        options.append(f"{label}{dist_info}")
                        option_details.append({
                            "name": cand.get("name", ""),
                            "lat": cand.get("lat", 0),
                            "lon": cand.get("lon", 0),
                            "address": cand.get("address", ""),
                        })
                    ambiguous_points.append({
                        "route_index": idx,
                        "point_index": pt_idx,
                        "query": d.get("query", ""),
                        "status": "needs_confirm",
                        "options": options,
                        "option_details": option_details
                    })

        needs_interaction = {
            "has_ambiguous_pois": len(ambiguous_points) > 0,
            "ambiguous_points": ambiguous_points
        }
        
        # 改进1: 检查是否有 park_internal 路线，更新 content_type
        has_park_internal = any(r.get("content_type") == "park_internal" for r in processed_routes)
        final_content_type = "park_internal" if has_park_internal and len(processed_routes) == 1 else content_type
        
        # 改进4: P2b — 构建 _debug 字段
        _debug_info = {
            "parse_path": req_log["parse_path"],
            "total_ms": round((time.monotonic() - _req_start) * 1000),
            "ai_ms": round((_timing["ai_end"] - _timing["ai_start"]) * 1000) if _timing["ai_end"] > 0 else 0,
            "geocode_ms": round((_timing["geocode_end"] - _timing["geocode_start"]) * 1000) if _timing["geocode_end"] > 0 else 0,
            "point_count": req_log["point_count"],
            "cache_hits": req_log["cache_hits"],
        }
        
        # 4. 写入路线缓存并返回结果
        response_data = {
            "success": True,
            "content_type": final_content_type,
            "city": city,
            "inferred_city": city,  # AI/infer_context 推断的城市
            "route_count": len(processed_routes),
            "routes": processed_routes,
            "xhs_link": xhs_link,
            "xhs_title": xhs_info.get("title") if xhs_info else None,
            "needs_interaction": needs_interaction,
            "_debug": _debug_info if app.debug else None,
        }
        # 移除 None 的 _debug（非 debug 模式）
        if response_data.get("_debug") is None:
            response_data.pop("_debug", None)
        
        _set_route_cache(route_key, response_data)
        
        return _log_and_return(response_data)
        
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/confirm", methods=["POST"])
def confirm_selection():
    """
    用户确认歧义点后，用确认结果重新生成高德 URL。
    
    快捷指令流程：
    1. POST /parse -> 拿到结果
    2. 检查 needs_interaction.has_ambiguous_pois
    3. 如果有歧义点：
       for each ambiguous_point:
         "Choose from Menu" 展示 options 数组
         记录用户选择的 index
    4. POST /confirm 发送 selections -> 拿到新的 amap_url
    5. 如果没有歧义点：直接用 routes[0].amap_url
    6. Open URL -> 打开高德
    
    请求体：
    {
        "locations": [...],     # 原始 /parse 返回的 locations 列表
        "mode": 2,              # 路线模式
        "selections": {         # 歧义点选择
            "2": {              # 第2个点
                "lat": 30.233,
                "lon": 120.148,
                "name": "太子湾公园"
            }
        }
    }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"success": False, "error": "无请求数据"}), 400
        
        locations = data.get("locations", [])
        mode = data.get("mode", 2)
        selections = data.get("selections", {})
        
        if not locations:
            return jsonify({"success": False, "error": "缺少 locations"}), 400
        
        # 应用用户选择
        for idx_str, selection in selections.items():
            idx = int(idx_str)
            if 0 <= idx < len(locations):
                locations[idx] = {
                    "lat": selection["lat"],
                    "lon": selection["lon"],
                    "name": selection.get("name", locations[idx].get("name", ""))
                }
        
        # 重新生成高德 URL
        mode_map = {0: "drive", 1: "transit", 2: "walk", 3: "bike"}
        mode_str = mode_map.get(mode, "walk")
        
        coords = [(loc["lat"], loc["lon"]) for loc in locations if loc.get("lat") and loc.get("lon")]
        if len(coords) < 2:
            return jsonify({"success": False, "error": "有效坐标不足"}), 400
        
        amap_url = build_amap_url(locations, mode)
        
        return jsonify({
            "success": True,
            "amap_url": amap_url,
            "locations": locations,
            "point_count": len(coords)
        })
        
    except Exception as e:
        print(f"[confirm] 错误: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/cache/stats", methods=["GET"])
def cache_stats():
    """缓存统计信息"""
    try:
        stats = get_cache_stats()
        stats["emoji_learned"] = len(EMOJI_LIBRARY.get("learned", {}))
        stats["emoji_pending"] = len(EMOJI_LIBRARY.get("pending_review", {}))
        stats["route_cache_memory"] = len(_route_cache)
        stats["xhs_fetch"] = dict(_xhs_fetch_stats)
        return jsonify({"success": True, **stats})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    """健康检查接口"""
    return jsonify({"status": "ok", "version": APP_VERSION, **configuration_status()})


# ==================== 启动 ====================

if __name__ == "__main__":
    print("RouteSnap Server starting...")
    print("API: http://localhost:5001")
    app.run(
        host=os.environ.get("ROUTESNAP_HOST", "127.0.0.1"),
        port=int(os.environ.get("ROUTESNAP_PORT", "5001")),
        debug=False,
    )
