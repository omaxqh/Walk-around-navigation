"""
Emoji 自学习系统
功能：从用户输入中提取未知 emoji，基于路线信息回溯推断角色，并持久化学习结果

独立模块，不依赖 poi_disambiguate.py
"""

import re
import json
import os
import threading
from datetime import datetime

# ==================== 配置 ====================

EMOJI_LEARN_THRESHOLD = 0.80

# 文件写入锁（线程安全）
_file_lock = threading.Lock()

# Emoji Unicode 范围正则（覆盖常见表情、符号、旗帜等）
# 注意：精确定义范围，排除 CJK 汉字区间（U+4E00-U+9FFF）
EMOJI_PATTERN = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # Emoticons (表情)
    "\U0001F300-\U0001F5FF"  # Misc Symbols and Pictographs (杂项符号)
    "\U0001F680-\U0001F6FF"  # Transport and Map Symbols (交通地图)
    "\U0001F700-\U0001F77F"  # Alchemical Symbols
    "\U0001F780-\U0001F7FF"  # Geometric Shapes Extended
    "\U0001F800-\U0001F8FF"  # Supplemental Arrows-C
    "\U0001F900-\U0001F9FF"  # Supplemental Symbols and Pictographs
    "\U0001FA00-\U0001FA6F"  # Chess Symbols
    "\U0001FA70-\U0001FAFF"  # Symbols and Pictographs Extended-A
    "\U00002702-\U000027B0"  # Dingbats
    "\U000024C2-\U000024FF"  # Enclosed Alphanumerics (缩小范围，不包含汉字)
    "\U0000FE00-\U0000FE0F"  # Variation Selectors
    "\U0000200D"             # Zero Width Joiner
    "\U000020E3"             # Combining Enclosing Keycap
    "\U00002600-\U000026FF"  # Misc Symbols (天气等)
    "\U00002300-\U000023FF"  # Misc Technical
    "\U0000203C-\U0000204F"  # General Punctuation subset (缩小范围)
    "\U00002934-\U00002935"  # Arrows
    "\U000025AA-\U000025AB"  # Small squares
    "\U000025B6"             # Play button
    "\U000025C0"             # Reverse button
    "\U000025FB-\U000025FE"  # Medium squares
    "\U00002B05-\U00002B07"  # Arrows
    "\U00002B1B-\U00002B1C"  # Large squares
    "\U00002B50"             # Star
    "\U00002B55"             # Circle
    "\U00003030"             # Wavy dash
    "\U0000303D"             # Part alternation mark
    "\U0001F004"             # Mahjong
    "\U0001F0CF"             # Joker
    "\U0001F170-\U0001F171"  # A/B buttons
    "\U0001F17E-\U0001F17F"  # O/P buttons
    "\U0001F18E"             # AB button
    "\U0001F191-\U0001F19A"  # Squared words
    "\U0001F1E0-\U0001F1FF"  # Regional indicators (flags)
    "\U0001F201-\U0001F251"  # Squared CJK ideographs (these are emoji, not regular CJK)
    "]",
    flags=re.UNICODE
)


def load_emoji_library(library_path: str) -> dict:
    """
    加载 emoji 库文件
    如果没有 learned/pending_review 字段则初始化为空 dict
    """
    if not os.path.exists(library_path):
        return {
            "learned": {},
            "pending_review": {}
        }
    
    with open(library_path, 'r', encoding='utf-8') as f:
        library = json.load(f)
    
    # 确保有 learned 和 pending_review 字段
    if "learned" not in library:
        library["learned"] = {}
    if "pending_review" not in library:
        library["pending_review"] = {}
    
    return library


def get_all_known_emojis(library: dict) -> set:
    """
    从 library 中收集所有已知 emoji
    包括 connectors values、poi_categories values、route_keywords values、learned keys
    """
    known = set()
    
    # 1. connectors 中的所有 symbols
    connectors = library.get("connectors", {})
    for category_data in connectors.values():
        if isinstance(category_data, dict):
            # 普通 symbols 列表
            if "symbols" in category_data:
                known.update(category_data["symbols"])
            # numbers 分类有 circled, keycap, parenthesized
            for key in ["circled", "keycap", "parenthesized"]:
                if key in category_data:
                    known.update(category_data[key])
    
    # 2. poi_categories 中的所有 symbols
    poi_categories = library.get("poi_categories", {})
    for category_data in poi_categories.values():
        if isinstance(category_data, dict) and "symbols" in category_data:
            known.update(category_data["symbols"])
    
    # 3. route_keywords 中的值（通常是文字，但也检查一下）
    route_keywords = library.get("route_keywords", {})
    for key, value in route_keywords.items():
        if isinstance(value, list):
            for item in value:
                # 检查是否包含 emoji
                emojis = EMOJI_PATTERN.findall(str(item))
                known.update(emojis)
    
    # 4. learned 区域的 keys
    learned = library.get("learned", {})
    known.update(learned.keys())
    
    return known


def extract_emojis(text: str) -> list:
    """
    从文本中提取所有 emoji 及其上下文（前后各 10 个字符）
    返回格式: [{"emoji": "🌺", "position": 23, "context_before": "太子湾", "context_after": "打卡"}]
    """
    results = []
    
    for match in EMOJI_PATTERN.finditer(text):
        emoji = match.group()
        position = match.start()
        
        # 提取前后上下文（各10个字符）
        context_before = text[max(0, position - 10):position]
        context_after = text[match.end():match.end() + 10]
        
        results.append({
            "emoji": emoji,
            "position": position,
            "context_before": context_before,
            "context_after": context_after
        })
    
    return results


def find_unknown_emojis(emojis: list, library: dict) -> list:
    """
    对比已知库，返回库中不存在的 emoji 列表
    检查范围：connectors 所有 values、poi_categories 所有 values、route_keywords 所有 values、learned 区域
    """
    known = get_all_known_emojis(library)
    
    # 也检查 pending_review 区域
    pending_review = library.get("pending_review", {})
    known.update(pending_review.keys())
    
    unknown = []
    for emoji_info in emojis:
        emoji = emoji_info["emoji"]
        if emoji not in known:
            unknown.append(emoji_info)
    
    return unknown


def label_emojis_retroactively(original_text: str, route_info: dict, emojis: list) -> list:
    """
    基于 AI 已经输出的 route_info（包含 routes[0].points 地点列表），回溯推断每个 emoji 的角色
    
    规则（按优先级）：
    1. 出现在两个地点名之间 -> role="connector"，confidence=0.90
    2. 出现在行首/段首，或紧跟数字/序号 -> role="sequence"，confidence=0.85
    3. 紧贴在地点名前面 -> role="location_marker"，confidence=0.80
    4. 以上都不匹配 -> role="decoration"，confidence=0.70
    """
    results = []
    
    # 收集所有地点名
    all_points = []
    routes = route_info.get("routes", [])
    for route in routes:
        points = route.get("points", [])
        all_points.extend(points)
    
    # 构建地点名在文本中的位置索引
    point_positions = []
    for point in all_points:
        # 在文本中查找地点名出现的位置
        start = 0
        while True:
            idx = original_text.find(point, start)
            if idx == -1:
                break
            point_positions.append({
                "name": point,
                "start": idx,
                "end": idx + len(point)
            })
            start = idx + 1
    
    # 按位置排序
    point_positions.sort(key=lambda x: x["start"])
    
    for emoji_info in emojis:
        emoji = emoji_info["emoji"]
        position = emoji_info["position"]
        context_before = emoji_info.get("context_before", "")
        context_after = emoji_info.get("context_after", "")
        
        role = "decoration"
        confidence = 0.70
        evidence = "独立出现在描述文字中"
        
        # 规则1：出现在两个地点名之间
        # 检查 emoji 是否夹在两个地点之间
        prev_point = None
        next_point = None
        for pp in point_positions:
            if pp["end"] <= position:
                prev_point = pp
            if pp["start"] >= position + len(emoji) and next_point is None:
                next_point = pp
        
        if prev_point and next_point:
            # 检查 emoji 是否紧跟在前一个地点后面，且后面紧跟下一个地点
            gap_before = position - prev_point["end"]
            gap_after = next_point["start"] - (position + len(emoji))
            # 允许一些空白字符
            if gap_before <= 5 and gap_after <= 5:
                role = "connector"
                confidence = 0.90
                evidence = f"出现在地点 '{prev_point['name']}' 和 '{next_point['name']}' 之间"
        
        # 规则2：出现在行首/段首，或紧跟数字/序号
        if role == "decoration":
            # 检查是否在行首
            text_before_on_line = ""
            line_start = original_text.rfind('\n', 0, position)
            if line_start == -1:
                line_start = 0
            else:
                line_start += 1
            text_before_on_line = original_text[line_start:position].strip()
            
            # 行首或段首
            if position == 0 or original_text[position - 1] == '\n':
                role = "sequence"
                confidence = 0.85
                evidence = "出现在行首或段首"
            # 紧跟数字/序号
            elif re.match(r'^[\d①②③④⑤⑥⑦⑧⑨⑩❶❷❸❹❺❻❼❽❾❿1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣8️⃣9️⃣🔟\s\.、\)）]*$', text_before_on_line):
                role = "sequence"
                confidence = 0.85
                evidence = f"紧跟在序号后面: '{text_before_on_line}'"
        
        # 规则3：紧贴在地点名前面
        if role == "decoration":
            emoji_end = position + len(emoji)
            for pp in point_positions:
                # emoji 结束位置紧跟地点开始（允许0-2个字符的间隙）
                gap = pp["start"] - emoji_end
                if 0 <= gap <= 2:
                    role = "location_marker"
                    confidence = 0.80
                    evidence = f"紧贴在地点 '{pp['name']}' 前面"
                    break
        
        results.append({
            "emoji": emoji,
            "role": role,
            "confidence": confidence,
            "evidence": evidence
        })
    
    return results


def learn_emoji(emoji: str, role: str, confidence: float, library_path: str, context: str = None):
    """
    将新学到的 emoji 角色写入库文件
    
    入库规则：
    1. confidence >= 0.80 -> 写入 "learned" 区域
    2. 0.50 <= confidence < 0.80 -> 写入 "pending_review" 区域
    3. 如果 emoji 已存在：
       - 同角色：更新 seen_count+1, last_seen, 取最高 confidence
       - 不同角色：如果新角色的 confidence 更高，替换；否则只增加 seen_count
    """
    with _file_lock:
        # 读取当前库
        library = load_emoji_library(library_path)
        
        today = datetime.now().strftime("%Y-%m-%d")
        
        # 决定写入哪个区域
        if confidence >= EMOJI_LEARN_THRESHOLD:
            target_area = "learned"
        elif confidence >= 0.50:
            target_area = "pending_review"
        else:
            # 置信度太低，不入库
            return
        
        area = library.get(target_area, {})
        
        if emoji in area:
            # emoji 已存在
            existing = area[emoji]
            existing_role = existing.get("role", "")
            existing_confidence = existing.get("confidence", 0)
            
            if existing_role == role:
                # 同角色：更新 seen_count, last_seen, 取最高 confidence
                existing["seen_count"] = existing.get("seen_count", 1) + 1
                existing["last_seen"] = today
                existing["confidence"] = max(existing_confidence, confidence)
            else:
                # 不同角色
                if confidence > existing_confidence:
                    # 新角色置信度更高，替换
                    existing["role"] = role
                    existing["confidence"] = confidence
                    existing["last_seen"] = today
                    # 保留 seen_count 和 first_seen
                    existing["seen_count"] = existing.get("seen_count", 1) + 1
                else:
                    # 只增加 seen_count
                    existing["seen_count"] = existing.get("seen_count", 1) + 1
                    existing["last_seen"] = today
            
            # 更新 contexts（最多保留最近 5 个）
            if context:
                contexts = existing.get("contexts", [])
                if context not in contexts:
                    contexts.append(context)
                    if len(contexts) > 5:
                        contexts = contexts[-5:]
                    existing["contexts"] = contexts
        else:
            # 新 emoji，创建条目
            new_entry = {
                "role": role,
                "confidence": confidence,
                "seen_count": 1,
                "first_seen": today,
                "last_seen": today,
                "contexts": [context] if context else []
            }
            area[emoji] = new_entry
        
        library[target_area] = area
        
        # 写回文件
        with open(library_path, 'w', encoding='utf-8') as f:
            json.dump(library, f, ensure_ascii=False, indent=2)


def process_text_for_learning(text: str, route_info: dict, library_path: str) -> dict:
    """
    处理文本进行 emoji 学习的完整流程
    
    参数：
        text: 原始输入文本
        route_info: AI 输出的路线信息 {"routes": [{"points": [...]}]}
        library_path: emoji 库文件路径
    
    返回：
        {
            "total_emojis": int,
            "unknown_emojis": int,
            "learned": [...],
            "pending": [...]
        }
    """
    # 1. 加载库
    library = load_emoji_library(library_path)
    
    # 2. 提取所有 emoji
    all_emojis = extract_emojis(text)
    
    # 3. 找出未知 emoji
    unknown_emojis = find_unknown_emojis(all_emojis, library)
    
    # 4. 回溯标注
    labeled = label_emojis_retroactively(text, route_info, unknown_emojis)
    
    # 5. 学习并入库
    learned_list = []
    pending_list = []
    
    for label_info in labeled:
        emoji = label_info["emoji"]
        role = label_info["role"]
        confidence = label_info["confidence"]
        evidence = label_info["evidence"]
        
        learn_emoji(emoji, role, confidence, library_path, context=evidence)
        
        if confidence >= EMOJI_LEARN_THRESHOLD:
            learned_list.append(label_info)
        elif confidence >= 0.50:
            pending_list.append(label_info)
    
    return {
        "total_emojis": len(all_emojis),
        "unknown_emojis": len(unknown_emojis),
        "learned": learned_list,
        "pending": pending_list
    }


# ==================== 测试入口 ====================

if __name__ == "__main__":
    # 测试数据
    test_text = """
    杭州春天赏花攻略 🌸
    
    ①🌺太子湾 👉 茅家埠 👉 乌龟潭
    📍曲院风荷是必打卡点
    
    ②西湖南线：净慈寺 🌹 雷峰塔
    """
    
    test_route_info = {
        "routes": [
            {"name": "路线1", "points": ["太子湾", "茅家埠", "乌龟潭"]},
            {"name": "路线2", "points": ["曲院风荷"]},
            {"name": "路线3", "points": ["净慈寺", "雷峰塔"]}
        ]
    }
    
    # 测试提取
    emojis = extract_emojis(test_text)
    print("提取的 emoji:")
    for e in emojis:
        print(f"  {e['emoji']} @ {e['position']}: ...{e['context_before']}[{e['emoji']}]{e['context_after']}...")
    
    # 测试标注
    library_path = os.path.join(os.path.dirname(__file__), "config", "emoji_connector_library.json")
    library = load_emoji_library(library_path)
    unknown = find_unknown_emojis(emojis, library)
    print(f"\n未知 emoji 数量: {len(unknown)}")
    
    labeled = label_emojis_retroactively(test_text, test_route_info, unknown)
    print("\n标注结果:")
    for l in labeled:
        print(f"  {l['emoji']}: role={l['role']}, confidence={l['confidence']}, evidence={l['evidence']}")
