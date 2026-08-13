# -*- coding: utf-8 -*-
"""素材库随机选择器 — 从 jianying-editor skill 的 data/*.csv 素材库随机挑选转场/文字动画/画面特效/音效/配音人，并从本地表情包库随机选贴纸。

表情包匹配优先使用 assets/emoji_scenes.json 语义索引（tags + analysis.emotion），
索引缺失时回退按文件名关键词匹配。不凑合：情绪匹配不到返回 None。
"""
import csv
import json
import os
import random
import sys

# 定位 jianying-editor skill 根目录：环境变量优先，兜底常见安装位置。
# jianying-editor 是可选依赖（提供云素材库 csv / 原生贴纸），缺失时随机素材能力自动降级。
def _locate_jy_skill() -> str:
    env = os.environ.get("JY_SKILL_ROOT")
    if env and os.path.isdir(os.path.join(env, "scripts")):
        return env
    for cand in (
        os.path.expanduser(r"~/.config/opencode/skills/jianying-editor"),
        os.path.expanduser(r"~/.agents/skills/jianying-editor"),
    ):
        if os.path.isdir(os.path.join(cand, "scripts")):
            return cand
    return env or ""


SKILL_ROOT = _locate_jy_skill()
DATA_DIR = os.path.join(SKILL_ROOT, "data")

# 项目根目录（本脚本上一级）
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
EMOJI_DIR = os.path.join(PROJECT_ROOT, "assets", "emojis")

# emotion -> 音效标题关键词映射（cloud_sound_effects.csv 的 categories 字段为空，按 title 子串匹配）
EMOTION_SFX_KEYWORDS = {
    "angry": ["掌掴", "爆炸", "滚雷", "愤怒", "生气", "暴怒"],
    "happy": ["笑", "搞笑", "滑稽", "俏皮", "卡通", "欢乐", "登场"],
    "sad": ["低落", "哭", "难过", "疑问"],
    "surprise": ["疑惑", "烟花", "震惊", "惊讶", "嗯？", "你脑子"],
    "neutral": ["提示", "拍手", "木琴", "开机", "哇"],
}

# emotion -> 表情包匹配关键词（匹配 assets/emoji_scenes.json 的 tags + analysis.emotion，
# 同时兼容文件名关键词回退）。关键词覆盖本地索引中出现的细分情绪。
EMOTION_STICKER_KEYWORDS = {
    "angry": ["愤怒", "暴怒", "生气", "怒气", "火气", "气炸", "打脸", "补刀",
              "暴力威胁", "警告", "蔑视", "嫌弃", "嘲讽", "嘲笑", "阴阳怪气",
              "不耐烦", "制止", "霸气", "强颜欢笑", "给爷死"],
    "happy": ["开心", "快乐", "笑", "得意", "夸赞", "佩服", "庆祝", "赞同",
              "自信", "惊喜", "宠溺", "搞怪", "调侃", "社交", "看戏", "围观",
              "八卦", "好奇", "心动", "唤醒", "优秀", "666", "牛皮"],
    "sad": ["难过", "悲伤", "哭", "委屈", "卑微", "孤独", "沧桑", "无奈",
            "崩溃", "忧伤", "逃避", "可怜", "假装悲伤", "哀嚎", "低落", "emo",
            "羡慕", "柠檬", "没钱", "社死"],
    "surprise": ["震惊", "惊讶", "吃惊", "惊吓", "惊慌", "难以置信", "目瞪口呆",
                 "啊", "哇哦", "懵", "愣住", "怀疑人生", "不敢相信"],
    "neutral": ["思考", "迷茫", "无语", "敷衍", "认怂", "恳求", "正直", "端正",
                "谦虚", "冷静", "自信", "茫然", "不知所措", "发呆"],
}

# emoji_scenes.json 索引缓存（加载后固定）
_EMOJI_INDEX_CACHE = None

_CSV_CACHE = {}


def _load_csv(filename: str) -> list[dict]:
    """读取 skill data 下的 CSV，跳过 # 注释行，结果缓存到模块级。"""
    if filename in _CSV_CACHE:
        return _CSV_CACHE[filename]
    path = os.path.join(DATA_DIR, filename)
    rows = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
            reader = csv.DictReader(line for line in f if not line.lstrip().startswith("#"))
            for row in reader:
                if row and any(row.values()):
                    rows.append(row)
    _CSV_CACHE[filename] = rows
    return rows


def _random_identifier(filename: str, key: str) -> str:
    rows = _load_csv(filename)
    if not rows:
        return ""
    return random.choice(rows)[key].strip()


def random_transition() -> str:
    """返回 transitions.csv 随机 identifier（中文转场名，如"上移"）。"""
    return _random_identifier("transitions.csv", "identifier")


def random_text_animation() -> str:
    """返回 text_animations.csv 随机 identifier（中文文字动画名）。"""
    return _random_identifier("text_animations.csv", "identifier")


def random_scene_effect() -> str:
    """返回 video_scene_effects.csv 随机 identifier（中文画面特效名）。"""
    return _random_identifier("video_scene_effects.csv", "identifier")


def random_tts_speaker() -> str:
    """返回 tts_speakers.csv 随机 speaker_id。"""
    rows = _load_csv("tts_speakers.csv")
    if not rows:
        return "zh_male_huoli"
    return random.choice(rows)["speaker_id"].strip()


def sfx_for_emotion(emotion: str) -> dict | None:
    """按 emotion 从 cloud_sound_effects.csv 匹配一个音效 dict {effect_id,title,duration_s}。

    注意：该 CSV 的 categories 字段实际为空，改在 title 中做关键词匹配。
    """
    rows = _load_csv("cloud_sound_effects.csv")
    if not rows:
        return None
    keywords = EMOTION_SFX_KEYWORDS.get(emotion, [])
    candidates = []
    for row in rows:
        title = row.get("title", "")
        if any(kw in title for kw in keywords):
            candidates.append(row)
    if not candidates:
        return None
    row = random.choice(candidates)
    return {
        "effect_id": row.get("effect_id", "").strip(),
        "title": row.get("title", "").strip(),
        "duration_s": row.get("duration_s", "0").strip(),
    }


def random_sfx() -> dict:
    """任意随机音效 dict，返回 None 表示音效库为空。

    已弃用：流水线不再使用随机兜底音效（避免与情绪不匹配的凑合音效），
    保留仅供调试/演示。
    """
    return sfx_for_emotion("neutral") or sfx_for_emotion("happy")


def _load_emoji_index() -> list[dict] | None:
    """加载 assets/emoji_scenes.json 语义索引（缓存），缺失/损坏返回 None。"""
    global _EMOJI_INDEX_CACHE
    if _EMOJI_INDEX_CACHE is not None:
        return _EMOJI_INDEX_CACHE or None
    path = os.path.join(PROJECT_ROOT, "assets", "emoji_scenes.json")
    try:
        if not os.path.exists(path):
            _EMOJI_INDEX_CACHE = []
            return None
        with open(path, "r", encoding="utf-8") as f:
            idx = json.load(f)
        _EMOJI_INDEX_CACHE = idx if isinstance(idx, list) else []
        return _EMOJI_INDEX_CACHE or None
    except Exception as e:
        print(f"⚠️ 表情索引加载失败: {e}")
        _EMOJI_INDEX_CACHE = []
        return None


def _emoji_path(entry: dict) -> str | None:
    """返回索引条目的实际文件绝对路径（优先使用本地 EMOJI_DIR，filepath 仅作兜底）。"""
    fn = (entry.get("filename") or "").strip()
    if fn:
        # 优先检查本地 EMOJI_DIR
        cand = os.path.join(EMOJI_DIR, fn)
        if os.path.exists(cand):
            return cand
    # 本地没有再尝试 filepath（旧索引路径）
    fp = (entry.get("filepath") or "").strip()
    if fp and os.path.exists(fp):
        return fp
    return None


def _pick_from_index(idx: list, emotion: str) -> str | None:
    """按语义索引匹配：将 tags + analysis.emotion + filename 拼合打分，取最高分随机。"""
    keywords = EMOTION_STICKER_KEYWORDS.get(emotion, [])
    if not keywords:
        # 该情绪不挑贴纸（如 neutral 无匹配语义时），任意选一个可用的
        paths = [p for e in idx if (p := _emoji_path(e))]
        return random.choice(paths) if paths else None
    scored = []
    for e in idx:
        p = _emoji_path(e)
        if not p:
            continue
        analysis = e.get("analysis") or {}
        text = " ".join([
            e.get("filename", ""),
            analysis.get("emotion", ""),
            " ".join(e.get("tags", []) or []),
        ])
        score = sum(1 for kw in keywords if kw in text)
        if score > 0:
            scored.append((score, p))
    if not scored:
        return None  # 情绪不匹配：不凑合
    top = max(s for s, _ in scored)
    best = [p for s, p in scored if s == top]
    return random.choice(best)


def pick_sticker(emotion: str = "neutral") -> str | None:
    """从本地表情包库按情绪挑贴纸，返回图片绝对路径或 None（不凑合）。

    优先用 assets/emoji_scenes.json 语义索引（tags/analysis.emotion 匹配），
    索引缺失时回退按文件名关键词匹配。emotion 无关键词时任意挑选。
    """
    idx = _load_emoji_index()
    if idx:
        return _pick_from_index(idx, emotion)
    # 回退：按文件名关键词匹配
    if not os.path.isdir(EMOJI_DIR):
        return None
    exts = (".png", ".jpg", ".jpeg", ".gif", ".webp")
    files = [f for f in os.listdir(EMOJI_DIR) if f.lower().endswith(exts)]
    if not files:
        return None
    keywords = EMOTION_STICKER_KEYWORDS.get(emotion, [])
    if not keywords:
        # 该情绪不挑贴纸（如 neutral），任意选一个
        return os.path.join(EMOJI_DIR, random.choice(files))
    matched = [f for f in files if any(kw in f for kw in keywords)]
    if not matched:
        return None  # 情绪不匹配：不凑合
    return os.path.join(EMOJI_DIR, random.choice(matched))


if __name__ == "__main__":
    print("转场:", random_transition())
    print("文字动画:", random_text_animation())
    print("画面特效:", random_scene_effect())
    print("配音人:", random_tts_speaker())
    print("愤怒音效:", sfx_for_emotion("angry"))
    print("随机音效:", random_sfx())
    print("表情包:", pick_sticker("angry"))
    print("表情包(中性):", pick_sticker("neutral"))
