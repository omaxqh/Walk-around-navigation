"""
POI消歧模块 v2.0

将景点名称（可能存在歧义）通过多层缓存 + 候选召回 + 智能打分 转换为唯一坐标。

架构设计（请求命中顺序）：
  [L1 单点POI缓存] → [L2 热门POI本地库] → [L3 SingleFlight去重] → [L4 真实调用高德(限流)] → 写缓存 → 返回

核心策略：
  1. SingleFlight：同一个 key 同一时刻只发一次真实请求，其余等待者共享结果
  2. 全局限流：threading.Semaphore(3) 控制高德 API 总并发
  3. 超时预算：单点 POI ≤2.0s，整条路线 ≤8.0s，超时点标记为 needs_confirm
  4. 热门POI本地库：config/hot_pois.json 预置高频景点坐标，命中即 auto_accept
  5. 候选召回策略：按优先级依次尝试（原名+城市/区域、别名、周边搜索、兜底）
  6. 置信度分层：auto_accept(≥0.80) / needs_confirm(0.50–0.80) / failed(<0.50)

对外接口：
  disambiguate_route(names, text, city, mode) → list[dict]  # 新主接口
  geocode(place_name, city)                   → dict | None  # 向后兼容
  batch_geocode(names, city)                  → (locations, failed)  # 向后兼容
  clear_cache()
"""

import time
import math
from math import radians, sin, cos, sqrt, atan2
import json
import os
import re
import threading
import sqlite3
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional


# ==================== 配置 ====================

AMAP_KEY = os.getenv("AMAP_KEY", "")
AMAP_SEARCH_URL = "https://restapi.amap.com/v3/place/text"
AMAP_AROUND_URL = "https://restapi.amap.com/v3/place/around"

# 景区类 POI 类型码白名单（高德分类体系）
SCENIC_TYPES = "110000|110100|110101|110200|110203|140000|141200|141201|080500"

# 景区类后缀（用于判断地名是否已含后缀，避免重复添加）
SCENIC_SUFFIXES = ["公园", "景区", "广场", "风景名胜区", "风景区", "湖", "山", "寺", "塔", "博物馆", "纪念馆"]

# 路线内容类别配置（用于动态调整 POI 类型得分）
ROUTE_CATEGORIES = {
    "scenic": {
        "keywords": ["赏花", "赏樱", "赏秋", "看日落", "看夜景", "观景", "citywalk", "散步", "徒步", "hiking"],
        "type_boost": ["110000", "110100", "110200", "110201", "110202", "110203", "110204", "110205", "110206",
                        "110207", "110208", "110209", "110210", "110211", "110212", "110213"],
        "type_penalty": ["150000", "150100", "070000"]
    },
    "food": {
        "keywords": ["探店", "美食", "咖啡", "茶馆", "小吃", "觅食", "打卡餐厅", "吃货", "brunch"],
        "type_boost": ["050000", "050100", "050200", "050300", "050400"],
        "type_penalty": []
    },
    "culture": {
        "keywords": ["展览", "博物馆", "美术馆", "书店", "文创", "艺术", "历史", "古迹"],
        "type_boost": ["140000", "141200", "141201", "140100"],
        "type_penalty": []
    },
    "shopping": {
        "keywords": ["逛街", "购物", "商场", "买买买", "奥莱", "市集"],
        "type_boost": ["060000", "060100", "060200", "060300"],
        "type_penalty": []
    }
}

# 超时配置
TIMEOUT_PER_POI = 2.0
TIMEOUT_PER_ROUTE = 8.0

# 置信度阈值
CONFIDENCE_THRESHOLDS = {"auto_accept": 0.80, "needs_confirm": 0.50}

# 景区类型码前缀集合（用于打分）
SCENIC_TYPE_CODES = set(SCENIC_TYPES.split("|"))

# 低价值 POI 类型码前缀
LOW_VALUE_TYPES = {"150", "070", "120", "141"}  # 停车场、加油站、小区、公司


# ==================== 配置加载 ====================

_CONFIG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config")


def _load_json(filename, default=None):
    """加载 JSON 配置文件"""
    path = os.path.join(_CONFIG_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[配置] 加载失败: {filename}, {e}")
        return default if default is not None else {}


ALIAS_DICT = _load_json("alias_dict.json", {})
AREA_KEYWORDS = _load_json("area_keywords.json", {})
HOT_POIS = _load_json("hot_pois.json", {})


# ==================== SingleFlight ====================

class SingleFlight:
    """
    同一个 key 同一时刻只发一次真实请求，其余等待者共享结果。
    避免对同一个 POI 并发发起多次高德 API 调用。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._inflight = {}  # key -> {"event": Event, "result": Any, "error": Exception}

    def do(self, key, fn, *args, **kwargs):
        """
        执行去重调用。
        如果 key 已在飞行中，等待并共享结果；否则执行 fn 并广播结果。
        """
        timeout = kwargs.pop("_sf_timeout", 5.0)

        with self._lock:
            if key in self._inflight:
                # 有正在进行的请求，等待结果
                flight = self._inflight[key]
                event = flight["event"]
            else:
                # 注册新请求
                event = threading.Event()
                flight = {"event": event, "result": None, "error": None}
                self._inflight[key] = flight
                event = None  # 标记为首发请求

        if event is not None:
            # 等待者：等待首发请求完成
            got = event.wait(timeout=timeout)
            if not got:
                raise TimeoutError(f"SingleFlight 等待超时: {key}")
            if flight["error"]:
                raise flight["error"]
            return flight["result"]

        # 首发请求：执行函数
        try:
            result = fn(*args, **kwargs)
            with self._lock:
                self._inflight[key]["result"] = result
            return result
        except Exception as e:
            with self._lock:
                self._inflight[key]["error"] = e
            raise
        finally:
            with self._lock:
                self._inflight[key]["event"].set()
                # 延迟清理，让等待者有时间获取结果
                def _cleanup():
                    time.sleep(0.1)
                    with self._lock:
                        if key in self._inflight:
                            del self._inflight[key]
                threading.Thread(target=_cleanup, daemon=True).start()


# 模块级实例
_poi_singleflight = SingleFlight()


# ==================== AmapClient 统一客户端 ====================

class AmapClient:
    """
    高德 API 统一客户端。
    封装：key 管理、全局限流(semaphore)、超时、结构化统计。
    所有高德 API 调用必须通过此客户端。
    """
    
    def __init__(self, api_key: str, max_concurrent: int = 3):
        self.api_key = api_key
        self._semaphore = threading.Semaphore(max_concurrent)
        self._stats = {"calls": 0, "success": 0, "errors": 0, "timeouts": 0}
        self._lock = threading.Lock()
    
    def _call(self, url: str, params: dict, timeout: float = 3.0) -> dict:
        """
        底层 API 调用（限流 + 超时 + 统计）。
        """
        with self._lock:
            self._stats["calls"] += 1
        
        acquired = self._semaphore.acquire(timeout=timeout)
        if not acquired:
            print(f"[AmapClient] 并发已满，等待超时")
            with self._lock:
                self._stats["timeouts"] += 1
            return None
        try:
            response = requests.get(url, params=params, timeout=min(timeout, 5))
            data = response.json()
            with self._lock:
                self._stats["success"] += 1
            return data
        except requests.Timeout:
            print(f"[AmapClient] 请求超时: {params.get('keywords', params.get('location', ''))}")
            with self._lock:
                self._stats["timeouts"] += 1
            return None
        except Exception as e:
            print(f"[AmapClient] 请求失败: {e}")
            with self._lock:
                self._stats["errors"] += 1
            return None
        finally:
            self._semaphore.release()
    
    def keyword_search(self, keyword: str, city: str = "", types: str = "",
                       offset: int = 5, timeout: float = 3.0) -> list:
        """关键词搜索 /v3/place/text，返回解析后的 POI 列表"""
        params = {
            "key": self.api_key,
            "keywords": keyword,
            "output": "json",
            "offset": offset,
        }
        if city:
            params["city"] = city
            params["city_limit"] = "true"
        if types:
            params["types"] = types
        
        data = self._call(AMAP_SEARCH_URL, params, timeout)
        return _parse_pois(data, f"keyword:{keyword}")
    
    def around_search(self, location: tuple, keyword: str = "", types: str = "",
                      radius: int = 5000, timeout: float = 3.0) -> list:
        """周边搜索 /v3/place/around，以 location=(lat, lon) 为中心"""
        params = {
            "key": self.api_key,
            "location": f"{location[1]},{location[0]}",  # 高德要 lon,lat
            "radius": radius,
            "output": "json",
            "offset": 5,
        }
        if keyword:
            params["keywords"] = keyword
        if types:
            params["types"] = types
        
        data = self._call(AMAP_AROUND_URL, params, timeout)
        return _parse_pois(data, f"around:{keyword}")
    
    @property
    def stats(self) -> dict:
        """返回调用统计（线程安全副本）"""
        with self._lock:
            return dict(self._stats)


# 模块级实例
_amap_client = AmapClient(AMAP_KEY)


def _call_amap_api(url, params, timeout=3.0):
    """向后兼容：代理到 AmapClient"""
    return _amap_client._call(url, params, timeout)


# ==================== SQLite 持久化缓存 ====================

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_DATA_DIR = os.path.join(_BASE_DIR, ".runtime")
_DATA_DIR = os.environ.get("ROUTESNAP_DATA_DIR", _DEFAULT_DATA_DIR).strip() or _DEFAULT_DATA_DIR
os.makedirs(_DATA_DIR, exist_ok=True)
_CACHE_DB = os.path.join(_DATA_DIR, "cache.db")


def _init_cache_db():
    """初始化 SQLite 缓存数据库"""
    conn = sqlite3.connect(_CACHE_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS poi_cache (
            key TEXT PRIMARY KEY,
            value TEXT,
            created_at REAL,
            hit_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS route_cache (
            key TEXT PRIMARY KEY,
            value TEXT,
            created_at REAL,
            hit_count INTEGER DEFAULT 0
        )
    """)
    conn.commit()
    conn.close()


def _sqlite_cache_get(table: str, key: str) -> Optional[dict]:
    """从 SQLite 读取缓存，命中时更新 hit_count"""
    try:
        conn = sqlite3.connect(_CACHE_DB)
        row = conn.execute(f"SELECT value FROM {table} WHERE key = ?", (key,)).fetchone()
        if row:
            conn.execute(f"UPDATE {table} SET hit_count = hit_count + 1 WHERE key = ?", (key,))
            conn.commit()
            conn.close()
            return json.loads(row[0])
        conn.close()
    except Exception as e:
        print(f"[SQLite] 读取失败: {e}")
    return None


def _sqlite_cache_set(table: str, key: str, value: dict):
    """写入 SQLite 缓存"""
    try:
        conn = sqlite3.connect(_CACHE_DB)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} (key, value, created_at, hit_count) VALUES (?, ?, ?, 0)",
            (key, json.dumps(value, ensure_ascii=False), time.time())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[SQLite] 写入失败: {e}")


def _sqlite_cleanup(max_age_days: int = 30):
    """清理过期缓存条目"""
    cutoff = time.time() - max_age_days * 86400
    try:
        conn = sqlite3.connect(_CACHE_DB)
        for table in ("poi_cache", "route_cache"):
            conn.execute(f"DELETE FROM {table} WHERE created_at < ? AND hit_count < 3", (cutoff,))
        conn.commit()
        conn.close()
        print("[SQLite] 缓存清理完成")
    except Exception as e:
        print(f"[SQLite] 清理失败: {e}")


def _sqlite_warmup(limit: int = 100):
    """启动时从 SQLite 预热高频缓存到内存"""
    try:
        conn = sqlite3.connect(_CACHE_DB)
        rows = conn.execute(
            "SELECT key, value FROM poi_cache ORDER BY hit_count DESC LIMIT ?", (limit,)
        ).fetchall()
        for key, value in rows:
            _poi_cache[key] = json.loads(value)
        print(f"[SQLite] 预热 {len(rows)} 条 POI 缓存")
        conn.close()
    except Exception as e:
        print(f"[SQLite] 预热失败: {e}")


def get_cache_stats() -> dict:
    """返回缓存统计信息"""
    try:
        conn = sqlite3.connect(_CACHE_DB)
        poi_count = conn.execute("SELECT COUNT(*) FROM poi_cache").fetchone()[0]
        route_count = conn.execute("SELECT COUNT(*) FROM route_cache").fetchone()[0]
        conn.close()
    except:
        poi_count = route_count = -1
    stats = {
        "poi_cache_memory": len(_poi_cache),
        "poi_cache_sqlite": poi_count,
        "route_cache_sqlite": route_count,
        "hot_pois": sum(len(v) for v in HOT_POIS.values()) if HOT_POIS else 0,
    }
    stats["amap_api"] = _amap_client.stats
    return stats


# ==================== 内存缓存 ====================

_poi_cache = {}  # 单点 POI 缓存（内存层）

# 模块加载时初始化 SQLite 缓存
_init_cache_db()
_sqlite_cleanup()
_sqlite_warmup()


def clear_cache():
    """清空所有缓存（内存 + SQLite）"""
    global _poi_cache
    _poi_cache = {}
    try:
        conn = sqlite3.connect(_CACHE_DB)
        conn.execute("DELETE FROM poi_cache")
        conn.execute("DELETE FROM route_cache")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[SQLite] 清空失败: {e}")
    print("[缓存] 已清空所有 POI 缓存")


# ==================== 工具函数 ====================

def has_scenic_suffix(name: str) -> bool:
    """判断地名是否已包含景区类后缀"""
    return any(name.endswith(suffix) for suffix in SCENIC_SUFFIXES)


# 名称标准化正则
_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U0001f900-\U0001f9FF"
    "\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002600-\U000026FF"
    "\U0000200D\U0000FE0F]+")

_NOISE_PREFIX = re.compile(
    r'^(打卡|必去|建议先去|推荐去?|第[一二三四五六七八九十\d]+站[：:\s]*'
    r'|\d+[.\s、)）]\s*|[①②③④⑤⑥⑦⑧⑨⑩]\s*|\([0-9]+\)\s*)')


def normalize_query(raw: str) -> str:
    """
    清洗用户输入的地点名称。
    - 移除 emoji
    - 移除序号前缀（第一站、1.、①等）
    - 全角转半角
    - 统一括号
    """
    text = raw.strip()
    text = _EMOJI_RE.sub('', text)
    text = _NOISE_PREFIX.sub('', text)

    # 全角转半角（常见标点和字母数字）
    result = []
    for ch in text:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        elif ch == '\u3000':
            result.append(' ')
        else:
            result.append(ch)
    text = ''.join(result)

    # 统一括号
    text = text.replace('（', '(').replace('）', ')')
    text = text.replace('【', '[').replace('】', ']')
    return text.strip()


def _strip_suffix(name: str) -> str:
    """去掉常见景区后缀，提取核心词"""
    for sfx in SCENIC_SUFFIXES:
        if name.endswith(sfx) and len(name) > len(sfx):
            return name[:-len(sfx)]
    return name


def _edit_distance(s1: str, s2: str) -> int:
    """计算编辑距离（Levenshtein Distance）"""
    if len(s1) < len(s2):
        return _edit_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr = [i + 1]
        for j, c2 in enumerate(s2):
            curr.append(min(prev[j + 1] + 1, curr[j] + 1, prev[j] + (c1 != c2)))
        prev = curr
    return prev[len(s2)]


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    """计算两点间距离（公里），Haversine 公式"""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return R * 2 * math.asin(math.sqrt(a))


# ==================== 上下文推断 ====================

# 中国主要城市列表（用于从文案中识别城市）
KNOWN_CITIES = [
    "杭州", "北京", "上海", "广州", "深圳", "成都", "重庆", "武汉",
    "南京", "苏州", "西安", "长沙", "厦门", "青岛", "大连", "昆明",
    "三亚", "丽江", "桂林", "拉萨", "敦煌", "天津", "沈阳", "哈尔滨",
    "郑州", "济南", "福州", "南昌", "合肥", "贵阳", "海口", "银川",
    "兰州", "西宁", "太原", "石家庄", "长春", "呼和浩特", "乌鲁木齐",
    "南宁", "无锡", "宁波", "温州", "东莞", "佛山", "珠海", "中山"
]


def infer_context(text: str, names: list, user_city: str = "", mode: str = "walk") -> dict:
    """
    推断消歧上下文。

    参数:
        text: 用户输入的完整文案
        names: 清洗后的地点名称列表
        user_city: 用户指定的城市
        mode: 出行模式 (walk/bike/drive)

    返回:
        {"city": str, "area": str, "mode": str, "city_source": str}
    """
    # 城市优先级：user_city > 文案识别 > 默认"杭州"
    city = ""
    city_source = "default"

    if user_city:
        city = user_city
        city_source = "user"
    else:
        # 从文案和地点列表中扫描城市名
        combined = text + " " + " ".join(names)
        for c in KNOWN_CITIES:
            if c in combined:
                city = c
                city_source = "text"
                break

    if not city:
        city = "杭州"

    # 区域推断：扫描文案和地点列表
    area = ""
    combined = text + " " + " ".join(names)
    for keyword, area_name in AREA_KEYWORDS.items():
        if keyword in combined:
            area = area_name
            break

    # 推断路线类别
    category = "scenic"  # 默认
    text_lower = (text + " ".join(names)).lower()
    for cat, info in ROUTE_CATEGORIES.items():
        if any(kw in text_lower for kw in info["keywords"]):
            category = cat
            break

    return {"city": city, "area": area, "mode": mode, "city_source": city_source, "category": category}


# ==================== 热门 POI 查询 ====================

def _lookup_hot_poi(query: str, city: str) -> dict:
    """
    查询热门 POI 本地库。
    命中返回 POI dict，未命中返回 None。
    """
    city_pois = HOT_POIS.get(city, {})
    if not city_pois:
        # 尝试模糊匹配城市（如"杭州市"匹配"杭州"）
        for key in HOT_POIS:
            if key in city or city in key:
                city_pois = HOT_POIS[key]
                break

    poi = city_pois.get(query)
    if poi:
        print(f"[热门POI] 命中本地库: {query} -> {poi['name']}")
    return poi


# ==================== API 搜索 ====================

def _parse_pois(data: dict, source: str) -> list:
    """解析高德 API 返回的 POI 列表"""
    results = []
    if not data or data.get("status") != "1" or not data.get("pois"):
        return results

    for rank, poi in enumerate(data["pois"]):
        try:
            location = poi.get("location", "")
            if not location or "," not in location:
                continue
            lon, lat = location.split(",")
            results.append({
                "poi_id": poi.get("id", ""),
                "name": poi.get("name", ""),
                "lat": float(lat),
                "lon": float(lon),
                "address": poi.get("address", "") or "",
                "city": poi.get("cityname", "") or "",
                "district": poi.get("adname", "") or "",
                "type": poi.get("type", "") or "",
                "typecode": poi.get("typecode", "") or "",
                "raw_rank": rank,
                "source": source,
            })
        except Exception:
            continue
    return results


def _search_poi_list(keyword, city="", types="", offset=5, timeout=3.0) -> list:
    """关键词搜索 POI 列表"""
    return _amap_client.keyword_search(keyword, city, types or "", offset, timeout)


def _search_around(location, keyword="", types=None, radius=5000, timeout=3.0) -> list:
    """周边搜索"""
    return _amap_client.around_search(location, keyword, types or "", radius, timeout)


# ==================== 候选召回 ====================

def recall_candidates(query, city="", area="", prev_location=None,
                      aliases=None, topk=5, timeout=2.0) -> list:
    """
    多策略候选召回，合并去重，返回 topk 个候选。

    策略优先级：
    0. 道路类名称（以路/街/大道/巷结尾）先用道路类型码搜索
    1. 原名 + 城市 + SCENIC_TYPES
    2. 原名 + 区域 + SCENIC_TYPES
    3. 别名搜索
    4. 城市前缀 + 原名
    5. 周边搜索（如果有前一个点坐标）
    6. 兜底，不限类型
    """
    seen_ids = set()
    candidates = []

    def _add(items):
        for item in items:
            pid = item.get("poi_id", "")
            if pid and pid in seen_ids:
                continue
            if pid:
                seen_ids.add(pid)
            candidates.append(item)

    start = time.monotonic()

    def remaining():
        return max(0, timeout - (time.monotonic() - start))

    # 策略0: 道路类名称优先用道路类型码搜索（避免兜底搜到别墅/建筑等低价值POI）
    # 类型码: 190302=城市道路, 190301=自然地理, 190000=地名地址
    ROAD_ENDINGS = ("路", "街", "大道", "巷", "弄", "山路")
    if query.endswith(ROAD_ENDINGS) and remaining() > 0.3:
        _add(_search_poi_list(query, city=city, types="190302|190301|190000", timeout=remaining()))

    # 策略1: 原名 + 城市，不限类型（覆盖景区/道路/地名/交通设施/餐饮购物等一切可能类型）
    # 原限制 SCENIC_TYPES 会把道路、鈲道、地铁站等过滤掉，用户路线无法预测
    if remaining() > 0.3:
        _add(_search_poi_list(query, city=city, types=None, timeout=remaining()))

    # 策略2: 原名 + 区域 + SCENIC_TYPES
    if area and remaining() > 0.3 and len(candidates) < topk:
        _add(_search_poi_list(query, city=area, types=SCENIC_TYPES, timeout=remaining()))

    # 策略3: 别名搜索
    if aliases and remaining() > 0.3 and len(candidates) < topk:
        for alias in aliases[:2]:  # 最多搜2个别名
            if remaining() > 0.3:
                _add(_search_poi_list(alias, city=city, types=SCENIC_TYPES, timeout=remaining()))

    # 策略4: 城市前缀 + 原名
    if city and city not in query and remaining() > 0.3 and len(candidates) < topk:
        _add(_search_poi_list(f"{city}{query}", city=city, types=SCENIC_TYPES, timeout=remaining()))

    # 策略5: 周边搜索（如果有前一个点坐标）
    if prev_location and remaining() > 0.3 and len(candidates) < topk:
        _add(_search_around(prev_location, keyword=query, types=SCENIC_TYPES, timeout=remaining()))

    # 策略6: 兜底，不限类型
    if remaining() > 0.3 and len(candidates) < 2:
        _add(_search_poi_list(query, city=city, types=None, timeout=remaining()))

    return candidates[:topk]


# ==================== 候选打分 ====================

def name_score(query: str, candidate: dict) -> float:
    """名称匹配分 0-1"""
    poi_name = candidate.get("name", "")
    if not poi_name:
        return 0.0

    # 精确匹配
    if query == poi_name:
        return 1.0

    # 去后缀匹配
    q_core = _strip_suffix(query)
    p_core = _strip_suffix(poi_name)
    if q_core == p_core and len(q_core) >= 2:
        return 0.90

    # 包含匹配
    if query in poi_name or poi_name in query:
        return 0.80

    # 别名命中（由调用方标记）
    if candidate.get("_alias_match"):
        return 0.75

    # 核心词包含
    if len(q_core) >= 2 and q_core in poi_name:
        return 0.60
    if len(p_core) >= 2 and p_core in query:
        return 0.60

    # 编辑距离归一化
    max_len = max(len(query), len(poi_name), 1)
    dist = _edit_distance(query, poi_name)
    similarity = max(0, 1 - dist / max_len)
    return min(similarity * 0.50, 0.50)


def city_area_score(candidate: dict, context: dict) -> float:
    """城市区域匹配分 0-1"""
    target_city = context.get("city", "")
    target_area = context.get("area", "")
    poi_city = candidate.get("city", "")
    poi_district = candidate.get("district", "")
    poi_address = candidate.get("address", "")

    in_city = target_city and (target_city in poi_city)
    in_area = target_area and (target_area in poi_address or target_area in poi_district)

    if in_city and in_area:
        return 1.0
    if in_city:
        return 0.70
    if not target_city:
        return 0.50  # 无城市约束时给中性分
    return 0.0


def type_score(candidate: dict, context: dict = None) -> float:
    """POI 类型分 0-1，根据路线类别动态调整
    
    覆盖高德全类型体系，不局限于景区，路线中可能出现道路/地铁站/餐饮/购物等。
    """
    typecode = candidate.get("typecode", "")
    type_str = candidate.get("type", "")

    # 获取路线类别配置
    category = (context or {}).get("category", "scenic")
    cat_info = ROUTE_CATEGORIES.get(category, ROUTE_CATEGORIES["scenic"])

    # 类型增强（路线类别匹配）
    if typecode and any(typecode.startswith(t) for t in cat_info.get("type_boost", [])):
        return 1.0
    # 类型惩罚
    if typecode and any(typecode.startswith(t) for t in cat_info.get("type_penalty", [])):
        return 0.10

    # 景区白名单（SCENIC_TYPE_CODES）
    if typecode and any(typecode.startswith(code[:3]) for code in SCENIC_TYPE_CODES):
        return 0.90

    # 景区相关关键词
    scenic_keywords = ["风景", "公园", "景区", "名胜", "博物馆", "纪念馆", "遗址"]
    if any(kw in type_str for kw in scenic_keywords):
        return 0.80

    # 地名地址类（190xxx）：道路、隧道、路口、热点地名等，路线中常用地标
    if typecode and typecode.startswith("190"):
        return 0.75

    # 交通设施类（150xxx）：地铁站、火车站、汽车站等，路线常见起终点
    if typecode and typecode.startswith("150") and any(
        kw in type_str for kw in ["地铁", "火车", "汽车", "轮渡", "机场", "公交枢纽"]
    ):
        return 0.70

    # 休闲娱乐类（060xxx）：夜市、游乐场、江边夜游等
    if typecode and typecode.startswith("060"):
        return 0.65

    # 美食类（010xxx）：美食路线常用
    if typecode and typecode.startswith("010"):
        return 0.60

    # 文化类扩展（展览、美术、书店等）
    if any(kw in type_str for kw in ["文化", "教育", "展览", "美术"]):
        return 0.60

    # 购物类（030xxx）：购物路线常用
    if typecode and typecode.startswith("030"):
        return 0.55

    # 体育运动类（070xxx）：徒步/骑行路线常用
    if typecode and typecode.startswith("070"):
        return 0.55

    # 明确低价值类（小区建筑群、公司企业）
    if typecode and any(typecode[:3] == lv for lv in {"120", "180"}):
        return 0.10

    # 其他已知类型给中性分
    known_prefixes = ["050", "080", "090", "100", "130", "140", "141", "160", "170"]
    if typecode and any(typecode.startswith(t) for t in known_prefixes):
        return 0.45

    return 0.30  # 未知类型给基础分


def route_score(candidate: dict, context: dict) -> float:
    """路线连续性分 0-1"""
    prev = context.get("prev_location")
    if not prev:
        return 0.70  # 首个点，中性分

    dist = _haversine_km(prev[0], prev[1], candidate["lat"], candidate["lon"])
    mode = context.get("mode", "walk")

    if mode == "walk":
        if dist < 2:
            return 1.0
        if dist < 5:
            return 0.60
        return 0.10
    elif mode == "bike":
        if dist < 5:
            return 1.0
        if dist < 15:
            return 0.60
        return 0.10
    else:  # drive
        if dist < 15:
            return 1.0
        if dist < 50:
            return 0.60
        return 0.10


def rank_score(candidate: dict, topk: int = 5) -> float:
    """高德排序位分 0-1"""
    raw_rank = candidate.get("raw_rank", 0)
    return max(0, 1.0 - raw_rank / max(topk, 1))


def score_candidate(query: str, candidate: dict, context: dict) -> float:
    """
    综合打分。
    权重：name=0.40, city=0.20, type=0.15, route=0.15, rank=0.10
    """
    ns = name_score(query, candidate)
    cs = city_area_score(candidate, context)
    ts = type_score(candidate, context)  # 传递 context 以支持类别动态调整
    rs = route_score(candidate, context)
    rk = rank_score(candidate)
    total = 0.40 * ns + 0.20 * cs + 0.15 * ts + 0.15 * rs + 0.10 * rk

    # 名称黑名单惩罚：候选名称含低价值词而 query 本身不含，大幅降权
    # 防止高德把「南山路210号别墅建筑」归类为风景名胜后得分虚高
    NOISY_WORDS = ["别墅", "建筑", "小区", "车库", "停车场", "XX号", "号别", "停车"]
    poi_name = candidate.get("name", "")
    if any(w in poi_name for w in NOISY_WORDS) and not any(w in query for w in NOISY_WORDS):
        total *= 0.3

    # 日志记录得分明细
    print(f"[打分] {query} -> {candidate.get('name', '')} | "
          f"name={ns:.2f} city={cs:.2f} type={ts:.2f} route={rs:.2f} rank={rk:.2f} | "
          f"total={total:.2f}")
    return total


# ==================== Beam Search 全局路径优化 ====================

def _transition_score(dist_km: float, mode: str) -> float:
    """相邻点转移得分（路线连续性奖惩）"""
    if mode == "walk":
        if dist_km < 2: return 0.3
        elif dist_km < 5: return 0.1
        elif dist_km < 10: return -0.2
        else: return -0.5
    elif mode == "bike":
        if dist_km < 5: return 0.3
        elif dist_km < 15: return 0.1
        elif dist_km < 30: return -0.2
        else: return -0.5
    else:  # drive
        if dist_km < 15: return 0.3
        elif dist_km < 50: return 0.1
        elif dist_km < 100: return -0.2
        else: return -0.5


def _beam_search_route(all_candidates: list, names: list, context: dict, beam_width: int = 3) -> list:
    """
    全局路径优化，保留 top-K 条路径。
    
    all_candidates[i] = 第 i 个点的候选列表（每个候选是 dict，含 score_details）
    返回：最优路径的 POI 列表
    """
    if not all_candidates:
        return []
    
    mode = context.get("mode", "walk")
    beam = [(0.0, [])]  # (score, path)
    
    for i, candidates in enumerate(all_candidates):
        if not candidates:
            # 没有候选，跳过这个点
            continue
        
        new_beam = []
        for prev_score, prev_path in beam:
            for cand in candidates[:beam_width]:
                # 计算转移得分
                transition = 0.0
                if prev_path:
                    last = prev_path[-1]
                    dist = _haversine_km(last["lat"], last["lon"], cand["lat"], cand["lon"])
                    transition = _transition_score(dist, mode)
                
                cand_score = cand.get("_total_score", 0)
                total = prev_score + cand_score + transition
                new_beam.append((total, prev_path + [cand]))
        
        # 保留 top beam_width
        new_beam.sort(key=lambda x: x[0], reverse=True)
        beam = new_beam[:beam_width]
    
    return beam[0][1] if beam else []


def _rebuild_results_from_beam(beam_path: list, names: list, context: dict) -> list:
    """从 Beam Search 结果重建 results 列表"""
    results = []
    for i, cand in enumerate(beam_path):
        if i < len(names):
            query = names[i]
        else:
            query = cand.get("name", "")
        
        score = cand.get("_total_score", 0)
        if score >= CONFIDENCE_THRESHOLDS["auto_accept"]:
            status = "auto_accept"
        elif score >= CONFIDENCE_THRESHOLDS["needs_confirm"]:
            status = "needs_confirm"
        else:
            status = "failed"
        
        results.append({
            "query": query,
            "raw_query": query,
            "selected_poi": {
                "name": cand.get("name", ""),
                "lat": cand["lat"],
                "lon": cand["lon"],
                "address": cand.get("address", ""),
                "city": cand.get("city", ""),
                "type": cand.get("type", ""),
            },
            "confidence": round(score, 4),
            "status": status,
            "candidates": [],
        })
    return results


# ==================== 距离二次验证 ====================

def haversine(loc1: str, loc2: str) -> float:
    """
    计算两点间距离（公里），Haversine 公式。
    
    参数:
        loc1: 坐标字符串，格式为 "lng,lat"
        loc2: 坐标字符串，格式为 "lng,lat"
    
    返回:
        距离（公里）
    """
    try:
        lon1, lat1 = map(float, loc1.split(","))
        lon2, lat2 = map(float, loc2.split(","))
    except (ValueError, AttributeError):
        return float('inf')  # 格式错误返回无穷大
    
    R = 6371  # 地球半径（公里）
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return R * c


def revalidate_by_distance(results: list) -> list:
    """
    低 confidence 距离二次验证。
    
    对 status == "needs_confirm" 且 confidence 在 [0.5, 0.7) 的点，
    通过与相邻 auto_accept 点的距离进行二次验证：
    - 距离 < 15km：提升 confidence 到 max(原值+0.15, 0.75)，status 改为 "auto_accept"
    - 距离 > 50km：降低 confidence 到 0.3，status 改为 "failed"
    - 中间范围（15-50km）不做改变
    
    参数:
        results: disambiguate_route 返回的结果列表
    
    返回:
        修改后的 results 列表
    """
    if not results:
        return results
    
    for i, r in enumerate(results):
        # 仅处理 needs_confirm 且 confidence 在 [0.5, 0.7) 的点
        if r.get("status") != "needs_confirm":
            continue
        conf = r.get("confidence", 0)
        if not (0.5 <= conf < 0.7):
            continue
        
        poi = r.get("selected_poi")
        if not poi or "lat" not in poi or "lon" not in poi:
            continue
        
        current_loc = f"{poi['lon']},{poi['lat']}"
        
        # 找最近的 auto_accept 邻居（前一个或后一个有效点）
        neighbor_loc = None
        
        # 向前找
        for j in range(i - 1, -1, -1):
            if results[j].get("status") == "auto_accept":
                npoi = results[j].get("selected_poi")
                if npoi and "lat" in npoi and "lon" in npoi:
                    neighbor_loc = f"{npoi['lon']},{npoi['lat']}"
                    break
        
        # 如果前面没找到，向后找
        if not neighbor_loc:
            for j in range(i + 1, len(results)):
                if results[j].get("status") == "auto_accept":
                    npoi = results[j].get("selected_poi")
                    if npoi and "lat" in npoi and "lon" in npoi:
                        neighbor_loc = f"{npoi['lon']},{npoi['lat']}"
                        break
        
        if not neighbor_loc:
            continue  # 没有 auto_accept 邻居，跳过
        
        # 计算距离
        dist = haversine(current_loc, neighbor_loc)
        
        if dist < 15:
            # 距离合理，提升置信度
            new_conf = max(conf + 0.15, 0.75)
            r["confidence"] = round(new_conf, 4)
            r["status"] = "auto_accept"
            r["revalidated"] = True
            print(f"[距离验证] {r['query']} 距离邻居 {dist:.1f}km < 15km，提升为 auto_accept (conf: {conf:.2f} -> {new_conf:.2f})")
        elif dist > 50:
            # 距离过远，降级
            r["confidence"] = 0.3
            r["status"] = "failed"
            r["revalidated"] = True
            print(f"[距离验证] {r['query']} 距离邻居 {dist:.1f}km > 50km，降级为 failed")
        # 15-50km 范围不做改变
    
    return results


# ==================== 公园内路线检测 ====================

# 园内子景点特征词
PARK_INTERNAL_KEYWORDS = re.compile(r"门|坡|池|亭|道|桥|广场|大道|花园|书院|步道|草坪|湖|溪|瀑布|喷泉|雕塑|牌坊|栈道|码头|观景台|游乐|风车|长廊")

# 父级景区名称模式
PARK_PARENT_PATTERN = re.compile(
    r"([\u4e00-\u9fa5]{2,10}(?:公园|景区|湿地|森林公园|植物园|动物园|乐园|花园))"
)


# 父级 POI 特征词（用于从匹配点名称中反推父级）
PARK_PARENT_FEATURE_PATTERN = re.compile(r"^([\u4e00-\u9fa5]{2,10}(?:公园|景区|湿地|森林公园|植物园|动物园|乐园))")


def detect_park_internal(results: list, names: list, text: str, city: str) -> list:
    """
    公园内路线检测。
    
    检测条件（同时满足）：
    1. failed + needs_confirm 的比例 > 50%
    2. POI 名称中有 >= 2 个包含园内子景点特征词
    
    父级提取回退策略（按顺序）：
    1. 正则提取 + 别名映射：从 text 提取候选名，查 alias_dict 映射
    2. 从成功匹配点反推：检查已匹配 POI 名称中是否含父级特征
    3. 从 names 中查热门 POI
    
    参数:
        results: disambiguate_route 返回的结果列表
        names: 原始地点名称列表（清洗后）
        text: 用户输入的完整文案
        city: 推断的城市
    
    返回:
        修改后的 results 列表（如果检测到公园内路线，第一个元素为父级 POI）
    """
    if not results or not names:
        return results
    
    # 统计匹配质量
    total = len(results)
    bad_count = sum(1 for r in results if r.get("status") in ("failed", "needs_confirm"))
    bad_ratio = bad_count / total if total > 0 else 0
    
    # 条件1: failed + needs_confirm 比例 > 50%
    if bad_ratio <= 0.5:
        return results
    
    # 统计包含园内子景点特征词的名称数量
    internal_count = sum(1 for name in names if PARK_INTERNAL_KEYWORDS.search(name))
    
    # 条件2: >= 2 个名称包含园内子景点特征词
    if internal_count < 2:
        return results
    
    print(f"[公园检测] 触发检测: bad_ratio={bad_ratio:.1%}, internal_count={internal_count}")
    
    # 尝试提取并确认父级 POI
    parent_poi = None
    parent_name_used = None  # 记录最终使用的父级名称
    
    # ========== 回退1: 正则提取 + 别名映射 ==========
    match = PARK_PARENT_PATTERN.search(text)
    if match:
        extracted_name = match.group(1)
        print(f"[公园检测] 从文案提取候选父级: {extracted_name}")
        
        # 尝试从别名库获取映射
        search_names = [extracted_name]  # 待搜索的名称列表
        if extracted_name in ALIAS_DICT:
            # 别名库命中，优先用映射后的正式名称
            mapped_names = ALIAS_DICT[extracted_name]
            print(f"[公园检测] 别名映射: {extracted_name} -> {mapped_names}")
            search_names = mapped_names + [extracted_name]  # 映射名优先
        
        # 按优先级尝试搜索
        for name_to_search in search_names:
            try:
                candidates = _amap_client.keyword_search(
                    name_to_search, city=city, types=SCENIC_TYPES, offset=3, timeout=2.0
                )
                if candidates:
                    parent_poi = candidates[0]
                    parent_name_used = name_to_search
                    print(f"[公园检测] 回退1成功: {name_to_search} -> {parent_poi['name']}")
                    break
            except Exception as e:
                print(f"[公园检测] 搜索失败: {name_to_search}, {e}")
                continue
    
    # ========== 回退2: 从成功匹配点反推父级 ==========
    if not parent_poi:
        print(f"[公园检测] 回退1未找到，尝试从成功匹配点反推父级")
        
        for r in results:
            # 只检查成功匹配的点
            if r.get("status") not in ("auto_accept", "needs_confirm"):
                continue
            
            selected_poi = r.get("selected_poi")
            if not selected_poi:
                continue
            
            # 获取匹配到的高德 POI 名称
            matched_name = selected_poi.get("name", "")
            if not matched_name:
                continue
            
            # 检查名称是否包含父级特征（如 "太子湾公园-放怀亭" -> 提取 "太子湾公园"）
            # 方式1: 检查是否有 "-" 分隔符
            if "-" in matched_name:
                parts = matched_name.split("-")
                for part in parts:
                    if PARK_PARENT_FEATURE_PATTERN.match(part):
                        candidate_parent = part.strip()
                        print(f"[公园检测] 从匹配点 '{matched_name}' 提取候选父级: {candidate_parent}")
                        # 搜索确认
                        try:
                            candidates = _amap_client.keyword_search(
                                candidate_parent, city=city, types=SCENIC_TYPES, offset=3, timeout=2.0
                            )
                            if candidates:
                                parent_poi = candidates[0]
                                parent_name_used = candidate_parent
                                print(f"[公园检测] 回退2成功: {candidate_parent} -> {parent_poi['name']}")
                                break
                        except Exception as e:
                            print(f"[公园检测] 搜索失败: {candidate_parent}, {e}")
                            continue
            
            # 方式2: 直接检查名称是否是父级特征
            if not parent_poi and PARK_PARENT_FEATURE_PATTERN.match(matched_name):
                candidate_parent = PARK_PARENT_FEATURE_PATTERN.match(matched_name).group(1)
                if candidate_parent != matched_name:  # 避免重复搜索完整名称
                    print(f"[公园检测] 从匹配点直接提取父级: {candidate_parent}")
                    try:
                        candidates = _amap_client.keyword_search(
                            candidate_parent, city=city, types=SCENIC_TYPES, offset=3, timeout=2.0
                        )
                        if candidates:
                            parent_poi = candidates[0]
                            parent_name_used = candidate_parent
                            print(f"[公园检测] 回退2成功: {candidate_parent} -> {parent_poi['name']}")
                            break
                    except Exception as e:
                        print(f"[公园检测] 搜索失败: {candidate_parent}, {e}")
            
            if parent_poi:
                break
    
    # ========== 回退3: 从 names 中查热门 POI ==========
    if not parent_poi:
        print(f"[公园检测] 回退2未找到，尝试从名称列表查热门POI")
        for name in names:
            # 检查是否命中热门 POI
            hot = _lookup_hot_poi(name, city)
            if hot:
                # 检查是否是景区类（名称中含公园、景区等关键词）
                if any(kw in name for kw in ["公园", "景区", "湿地", "植物园", "动物园", "乐园", "花园"]):
                    # 用热门 POI 作为父级
                    parent_poi = hot
                    parent_name_used = name
                    print(f"[公园检测] 回退3成功: 从名称列表发现父级: {name}")
                    break
    
    # 所有回退策略都失败
    if not parent_poi:
        print(f"[公园检测] 所有回退策略均未找到父级 POI，保持原结果")
        return results
    
    parent_location = f"{parent_poi['lon']},{parent_poi['lat']}"
    print(f"[公园检测] 确认父级 POI: {parent_poi.get('name', parent_name_used)} @ {parent_location}")
    
    # 构建父级 POI 的特殊标记元素
    park_parent_result = {
        "name": parent_poi.get("name", parent_name_used),
        "location": parent_location,
        "confidence": 0.95,
        "status": "auto_accept",
        "source": "park_parent",
        "park_internal": True,
        "internal_points": names,  # 原始子景点列表
    }
    
    # 将父级 POI 插入到结果列表的第 0 位
    return [park_parent_result] + results


# ==================== 主流程 ====================

def disambiguate_route(names: list, text: str = "", city: str = "", mode: str = "walk") -> list:
    """
    路线级 POI 消歧主入口。

    参数:
        names: 地点名称列表（原始输入）
        text: 用户输入的完整文案（用于上下文推断）
        city: 用户指定的城市
        mode: 出行模式 (walk/bike/drive)

    返回:
        list[dict]，每个 dict 包含：
        {
            "query": str,           # 原始输入（清洗后）
            "raw_query": str,       # 原始输入（清洗前）
            "selected_poi": dict,   # 选中的 POI（含 name/lat/lon/address/city/type）
            "confidence": float,    # 置信度 0-1
            "status": str,          # "auto_accept" / "needs_confirm" / "failed" / "timeout"
            "candidates": list,     # needs_confirm 时返回 top 3 候选
        }
    """
    route_start = time.monotonic()
    results = []
    all_candidates_collected = []  # 保存每个点的候选列表，供 Beam Search 使用
    cleaned_names_list = []  # 保存清洗后的名称列表

    # 1. 名称标准化
    cleaned_names = [(name, normalize_query(name)) for name in names]
    cleaned_names_list = [cn for _, cn in cleaned_names]

    # 2. 上下文推断
    context = infer_context(text, [cn for _, cn in cleaned_names], user_city=city, mode=mode)
    print(f"[消歧] 上下文: {context}")

    prev_location = None

    # 3. 逐点前向处理
    for raw_name, clean_name in cleaned_names:
        elapsed = time.monotonic() - route_start
        remaining = TIMEOUT_PER_ROUTE - elapsed

        # 超时降级
        if remaining <= 0.5:
            print(f"[消歧] 路线超时，剩余点降级: {clean_name}")
            results.append({
                "query": clean_name,
                "raw_query": raw_name,
                "selected_poi": None,
                "confidence": 0.0,
                "status": "timeout",
                "candidates": [],
            })
            continue

        point_timeout = min(TIMEOUT_PER_POI, remaining)

        # a. 查热门 POI 本地库
        hot_poi = _lookup_hot_poi(clean_name, context["city"])
        if hot_poi:
            # 为热门 POI 添加得分信息，供 Beam Search 使用
            hot_poi_with_score = {**hot_poi, "_total_score": 1.0}
            all_candidates_collected.append([hot_poi_with_score])
            results.append({
                "query": clean_name,
                "raw_query": raw_name,
                "selected_poi": hot_poi,
                "confidence": 1.0,
                "status": "auto_accept",
                "candidates": [],
            })
            prev_location = (hot_poi["lat"], hot_poi["lon"])
            continue

        # b. 查 POI 缓存（内存 -> SQLite）
        cache_key = f"poi:{context['city']}:{clean_name}"
        cached = None
        if cache_key in _poi_cache:
            cached = _poi_cache[cache_key]
            print(f"[缓存] 命中内存缓存: {clean_name}")
        else:
            # 内存 miss，尝试从 SQLite 读取
            cached = _sqlite_cache_get("poi_cache", cache_key)
            if cached:
                _poi_cache[cache_key] = cached  # 回填内存缓存
                print(f"[缓存] 命中 SQLite 缓存: {clean_name}")
        
        if cached:
            print(f"[缓存] 命中单点缓存: {clean_name}")
            # 缓存命中也需要计算 route_score（因为 prev_location 不同）
            scoring_ctx = {**context, "prev_location": prev_location}
            score = score_candidate(clean_name, cached, scoring_ctx)
            # 为缓存结果添加得分信息，供 Beam Search 使用
            cached_with_score = {**cached, "_total_score": score}
            all_candidates_collected.append([cached_with_score])
            status = "auto_accept" if score >= CONFIDENCE_THRESHOLDS["auto_accept"] else \
                "needs_confirm" if score >= CONFIDENCE_THRESHOLDS["needs_confirm"] else "failed"
            results.append({
                "query": clean_name,
                "raw_query": raw_name,
                "selected_poi": {
                    "name": cached["name"],
                    "lat": cached["lat"],
                    "lon": cached["lon"],
                    "address": cached.get("address", ""),
                    "city": cached.get("city", ""),
                    "type": cached.get("type", ""),
                },
                "confidence": round(score, 4),
                "status": status,
                "candidates": [],
            })
            prev_location = (cached["lat"], cached["lon"])
            continue

        # c. singleflight + 候选召回
        aliases = ALIAS_DICT.get(clean_name, [])
        sf_key = f"sf:{context['city']}:{clean_name}"

        try:
            candidates = _poi_singleflight.do(
                sf_key,
                recall_candidates,
                clean_name,
                city=context["city"],
                area=context["area"],
                prev_location=prev_location,
                aliases=aliases,
                topk=5,
                timeout=point_timeout,
                _sf_timeout=point_timeout + 0.5,
            )
        except Exception as e:
            print(f"[消歧] 召回异常: {clean_name}, {e}")
            candidates = []

        if not candidates:
            all_candidates_collected.append([])  # 空候选列表
            results.append({
                "query": clean_name,
                "raw_query": raw_name,
                "selected_poi": None,
                "confidence": 0.0,
                "status": "failed",
                "candidates": [],
            })
            continue

        # d. 打分
        scoring_ctx = {**context, "prev_location": prev_location}
        # 标记别名命中
        alias_names = set()
        for a in aliases:
            alias_names.add(a)
        for cand in candidates:
            if cand["name"] in alias_names:
                cand["_alias_match"] = True

        scored = []
        for cand in candidates:
            s = score_candidate(clean_name, cand, scoring_ctx)
            cand["_total_score"] = s  # 保存得分供 Beam Search 使用
            scored.append((s, cand))
        scored.sort(key=lambda x: x[0], reverse=True)
        
        # 保存候选列表供 Beam Search 使用
        all_candidates_collected.append([c for _, c in scored])

        # e. 选 top1
        best_score, best_cand = scored[0]

        # f. 置信度分层
        if best_score >= CONFIDENCE_THRESHOLDS["auto_accept"]:
            status = "auto_accept"
        elif best_score >= CONFIDENCE_THRESHOLDS["needs_confirm"]:
            status = "needs_confirm"
        else:
            status = "failed"

        # 写入缓存（内存 + SQLite，只缓存成功结果）
        if best_score >= CONFIDENCE_THRESHOLDS["needs_confirm"]:
            _poi_cache[cache_key] = best_cand
            _sqlite_cache_set("poi_cache", cache_key, best_cand)

        # 返回结果
        top3 = [{
            "name": c.get("name", ""),
            "lat": c["lat"],
            "lon": c["lon"],
            "address": c.get("address", ""),
            "city": c.get("city", ""),
            "type": c.get("type", ""),
            "score": round(s, 4)
        } for s, c in scored[:3]]

        results.append({
            "query": clean_name,
            "raw_query": raw_name,
            "selected_poi": {
                "name": best_cand["name"],
                "lat": best_cand["lat"],
                "lon": best_cand["lon"],
                "address": best_cand.get("address", ""),
                "city": best_cand.get("city", ""),
                "type": best_cand.get("type", ""),
            },
            "confidence": round(best_score, 4),
            "status": status,
            "candidates": top3 if status == "needs_confirm" else [],
        })

        prev_location = (best_cand["lat"], best_cand["lon"])

    # 4. Beam Search 全局优化（当贪心结果置信度较低时触发）
    if results and all_candidates_collected:
        valid_results = [r for r in results if r.get("confidence", 0) > 0]
        if valid_results:
            avg_conf = sum(r["confidence"] for r in valid_results) / len(valid_results)
            if avg_conf < 0.75:
                print(f"[Beam] 贪心平均置信度 {avg_conf:.2f} < 0.75，启用 Beam Search")
                beam_path = _beam_search_route(all_candidates_collected, cleaned_names_list, context)
                if beam_path and len(beam_path) == len(cleaned_names_list):
                    # 用 beam 结果重建 results
                    beam_results = _rebuild_results_from_beam(beam_path, cleaned_names_list, context)
                    beam_valid = [r for r in beam_results if r.get("confidence", 0) > 0]
                    if beam_valid:
                        beam_avg = sum(r["confidence"] for r in beam_valid) / len(beam_valid)
                        if beam_avg > avg_conf:
                            print(f"[Beam] Beam Search 结果更优: {beam_avg:.2f} > {avg_conf:.2f}")
                            results = beam_results

    # 5. 低 confidence 距离二次验证（P2a）
    results = revalidate_by_distance(results)

    # 6. 公园内路线检测（P0）
    results = detect_park_internal(results, cleaned_names_list, text, context["city"])

    return results


# ==================== 向后兼容接口 ====================

def geocode(place_name: str, city: str = "") -> dict:
    """
    向后兼容：单点地理编码。

    参数:
        place_name: 地点名称
        city: 城市限定

    返回:
        dict (含 name/lat/lon/address/city/type) 或 None
    """
    results = disambiguate_route([place_name], city=city)
    if results and results[0].get("selected_poi"):
        return results[0]["selected_poi"]
    return None


def batch_geocode(names: list, city: str = "") -> tuple:
    """
    向后兼容：批量地理编码。

    参数:
        names: 地点名称列表
        city: 城市限定

    返回:
        (locations: list[dict], failed: list[str])
    """
    if not names:
        return [], []

    results = disambiguate_route(names, city=city)
    locations = []
    failed = []

    for r in results:
        poi = r.get("selected_poi")
        if poi and r["status"] != "failed":
            locations.append(poi)
        else:
            failed.append(r["query"])

    return locations, failed


def build_amap_url(coords: list, mode: str = "walk") -> str:
    """
    生成高德地图多途经点导航 URL。
    
    参数:
        coords: [(lat, lon), ...] 坐标列表，至少 2 个点
        mode: "walk" / "drive" / "bike"
    
    返回: 高德导航 URL 字符串
    """
    if len(coords) < 2:
        return None
    
    # 模式映射（高德 URI API 使用的模式名称）
    mode_map = {
        "walk": "walk",
        "drive": "car",
        "bike": "ride",
        # 兼容其他可能的输入
        "car": "car",
        "ride": "ride",
    }
    amap_mode = mode_map.get(mode, "walk")
    
    # 起点和终点
    start = coords[0]
    end = coords[-1]
    
    # 高德 URL 格式：lon,lat（注意顺序）
    url = f"https://uri.amap.com/navigation?from={start[1]},{start[0]}&to={end[1]},{end[0]}"
    
    # 途经点（如果有）
    if len(coords) > 2:
        via_points = coords[1:-1]
        via_str = ";".join(f"{pt[1]},{pt[0]}" for pt in via_points)
        url += f"&via={via_str}"
    
    url += f"&mode={amap_mode}"
    
    return url


def _do_cluster(pois: list, threshold: float) -> list:
    """
    贪心聚类辅助函数。
    
    算法：
    - 取第一个未分组的 POI 作为种子
    - 将所有与种子或组内任意已有成员距离 < threshold 的 POI 加入同组
    - 重复直到所有 POI 都分组
    
    参数:
        pois: POI 列表，每个元素是 dict，必须包含 "location" 字段（格式 "lng,lat"）
        threshold: 聚类距离阈值（公里）
    
    返回: [[poi1, poi2, ...], [poi3, ...], ...]  每组是一个 POI 列表
    """
    if not pois:
        return []
    
    assigned = [False] * len(pois)
    clusters = []
    
    for i in range(len(pois)):
        if assigned[i]:
            continue
        # 新建一个簇，以 pois[i] 为种子
        cluster = [pois[i]]
        assigned[i] = True
        
        # 不断扫描未分配的点，看是否与簇内任意点距离 < threshold
        changed = True
        while changed:
            changed = False
            for j in range(len(pois)):
                if assigned[j]:
                    continue
                for member in cluster:
                    dist = haversine(pois[j]["location"], member["location"])
                    if dist < threshold:
                        cluster.append(pois[j])
                        assigned[j] = True
                        changed = True
                        break
        
        clusters.append(cluster)
    
    return clusters


def cluster_pois_by_distance(pois: list) -> list:
    """
    将 POI 按地理距离做两层聚类，返回带 mode 标记的分组结果。
    
    第一层：步行聚类（3km 阈值）
    - 所有点间距 < 3km 的聚成一簇
    - ≥ 2 点的簇 → 标记为 walk
    
    第二层：驾车聚类（30km 阈值）
    - 第一层中未进入任何 ≥2 点步行簇的散点
    - 这些散点之间做 30km 聚类
    - ≥ 2 点的簇 → 标记为 drive
    
    1 点的簇（无论哪层）→ 丢弃
    
    参数:
        pois: POI 列表，每个元素是 dict，必须包含 "location" 字段（格式 "lng,lat"）和 "name" 字段
    
    返回: [
        {"mode": "walk", "pois": [poi1, poi2, ...]},
        {"mode": "walk", "pois": [poi3, poi4]},
        {"mode": "drive", "pois": [poi5, poi6, poi7]},
    ]
    每组的 pois 已经过 sort_pois_nearest_neighbor 排序。
    """
    if not pois:
        return []
    
    result = []
    
    # 第一层：步行聚类（3km 阈值）
    walk_clusters = _do_cluster(pois, threshold=3)
    
    # 收集第一层中 size=1 的簇的 POI 作为散点
    scattered_pois = []
    for cluster in walk_clusters:
        if len(cluster) >= 2:
            # ≥2 点的簇标记为 walk，排序后加入结果
            sorted_cluster = sort_pois_nearest_neighbor(cluster)
            result.append({"mode": "walk", "pois": sorted_cluster})
        else:
            # size=1 的簇，收集为散点
            scattered_pois.extend(cluster)
    
    # 第二层：对散点做驾车聚类（30km 阈值）
    if scattered_pois:
        drive_clusters = _do_cluster(scattered_pois, threshold=30)
        for cluster in drive_clusters:
            if len(cluster) >= 2:
                # ≥2 点的簇标记为 drive，排序后加入结果
                sorted_cluster = sort_pois_nearest_neighbor(cluster)
                result.append({"mode": "drive", "pois": sorted_cluster})
            # size=1 的簇丢弃（不返回）
    
    return result


def sort_pois_nearest_neighbor(pois: list) -> list:
    """
    组内 POI 按最近邻贪心排序（TSP 近似解）。
    从第一个 POI 出发，每次选最近的未访问 POI。
    
    参数:
        pois: POI 列表，每个元素是 dict，必须包含 "location" 字段（格式 "lng,lat"）
    
    返回: 排序后的 POI 列表
    """
    if len(pois) <= 2:
        return pois
    
    remaining = list(range(1, len(pois)))
    order = [0]  # 从第一个点出发
    
    while remaining:
        current = order[-1]
        nearest_idx = None
        nearest_dist = float('inf')
        for idx in remaining:
            dist = haversine(pois[current]["location"], pois[idx]["location"])
            if dist < nearest_dist:
                nearest_dist = dist
                nearest_idx = idx
        order.append(nearest_idx)
        remaining.remove(nearest_idx)
    
    return [pois[i] for i in order]
