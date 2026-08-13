# -*- coding: utf-8 -*-
"""资产沉淀库（v6 工程化升级）—— 跨项目可复用模板与素材归档。

设计思想（来自《Codex Skill 全流程拆解》"资产沉淀"）：
  每条复刻/创作任务的拆解、剧本、音乐风格、表情包选择都应归档，
  做得越多可复用底子越厚。本模块把每次运行的关键产物写入 assets/library/，
  不依赖任何外部服务，纯本地 JSON 索引。

提供：
  - archive_project(dialogue)        把剧本关键字段归档成一条 library 记录
  - recommend_music(dialogue)        根据剧本特征推荐音乐引擎与曲风（Mureka/Suno）
  - MUSIC_STRATEGY / SUNO_PRESETS    音乐策略常量（文章2 的经验结论）
"""
from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

from .models import Dialogue

# ---------------------------------------------------------------------------
# 音乐策略（来自文章2 的实践结论："长音乐用 Mureka，短音乐用 Suno"）
# ---------------------------------------------------------------------------
# 推荐引擎：长叙事 / 完整故事 -> Mureka；短快节奏 -> Suno
MUSIC_STRATEGY = {
    "long_narrative": {
        "engine": "Mureka",
        "reason": "长音乐用 Mureka，适合完整叙事、情绪铺陈",
        "suno_style": "ballad",
    },
    "short_punchy": {
        "engine": "Suno",
        "reason": "短音乐用 Suno，节奏更紧凑，适合快节奏/魔性视频",
        "suno_style": "gospel_funk",
    },
}

# 魔性 BGM 预设（文章2 强调的"抖音魔性风格"）
SUNO_PRESETS = {
    "gospel_funk": "Gospel-infused funk, dual powerful black male lead vocals, raspy soulful vocal texture, "
                   "intricate melismatic riffs and gospel ad-libs, conversational call and response delivery, "
                   "lush gospel choir backing harmonies, tight slap bassline, chicken scratch rhythm guitar, "
                   "punchy brass section, play full dialogue ONLY ONCE, never loop or repeat content",
    "ballad": "Emotional ballad, soft piano and strings, male/female duet, gentle build, conversational and "
              "sincere delivery, play full dialogue ONLY ONCE",
    "rap": "Energetic hip-hop rap, punchy 808 bass, fast conversational flow, playful diss vibe, "
           "play full dialogue ONLY ONCE",
    "pop": "Catchy upbeat pop, bright synth, sing-along hook, lighthearted tone, "
           "play full dialogue ONLY ONCE",
    "catchy_meme": "Short looping comedic meme beat, quirky quirky synth, viral TikTok style, super punchy, "
                    "under 20 seconds, comedic timing, no lyrics, perfect for meme videos",
}

# 长/短剧本的简单判定阈值（消息数 or 总时长）
_LONG_MSG_THRESHOLD = 12
_LONG_DURATION_THRESHOLD = 45.0  # 秒


def recommend_music(dialogue: Dialogue) -> Dict[str, str]:
    """根据剧本特征推荐音乐引擎与曲风。

    返回 {engine, suno_style, reason, bgm_preset_key}
    - 长叙事（消息多 / 时长久）-> Mureka + ballad
    - 短快节奏（默认）-> Suno + gospel_funk
    - 若 dialogue.bgm_query 含"魔性/搞笑"等，优先 catchy_meme / gospel_funk
    """
    n = len(dialogue.messages)
    total = float(dialogue.meta.get("total_duration_s", 0.0) or 0.0)
    bgm = (dialogue.bgm_query or "").lower()

    # 魔性/搞笑偏好
    if any(k in bgm for k in ("魔性", "搞笑", "搞笑", "meme", "抖音", "病毒", "洗脑")):
        return {
            "engine": "Suno",
            "suno_style": "gospel_funk",
            "reason": "检测到魔性/搞笑 BGM 偏好，推荐 Suno 短快节奏",
            "bgm_preset_key": "gospel_funk",
        }

    is_long = (n >= _LONG_MSG_THRESHOLD) or (total >= _LONG_DURATION_THRESHOLD)
    if is_long:
        strat = MUSIC_STRATEGY["long_narrative"]
        return {
            "engine": strat["engine"],
            "suno_style": strat["suno_style"],
            "reason": strat["reason"] + f"（消息{n}条/时长{total:.0f}s，判定为长叙事）",
            "bgm_preset_key": strat["suno_style"],
        }
    strat = MUSIC_STRATEGY["short_punchy"]
    return {
        "engine": strat["engine"],
        "suno_style": strat["suno_style"],
        "reason": strat["reason"] + f"（消息{n}条/时长{total:.0f}s，判定为短快节奏）",
        "bgm_preset_key": strat["suno_style"],
    }


# ---------------------------------------------------------------------------
# 资产归档
# ---------------------------------------------------------------------------
def _library_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(here))  # scripts/engine -> project
    d = os.path.join(project_root, "assets", "library")
    os.makedirs(d, exist_ok=True)
    return d


def archive_project(dialogue: Dialogue, qa_result: Optional[dict] = None) -> str:
    """把本次剧本归档为一条 library 记录（轻量 JSON 索引）。

    不直接复制大文件，只记录可复用的结构化信息：标题、角色、情绪分布、
    音乐推荐、素材命中、QA 结果。返回归档文件路径。
    """
    rec = {
        "title": dialogue.title,
        "project_name": dialogue.project_name,
        "bgm_query": dialogue.bgm_query,
        "message_count": len(dialogue.messages),
        "roles": {},
        "emotion_dist": {},
        "music_recommend": recommend_music(dialogue),
        "sticker_hits": 0,
        "overlay_hits": 0,
        "sfx_hits": 0,
        "missing_assets": len(dialogue.meta.get("missing_assets", []) or []),
        "qa": {
            "ok": bool(qa_result.get("ok")) if qa_result else None,
            "errors": qa_result.get("stats", {}).get("error", 0) if qa_result else None,
            "warnings": qa_result.get("stats", {}).get("warning", 0) if qa_result else None,
        },
    }
    for m in dialogue.messages:
        rec["roles"][m.role] = rec["roles"].get(m.role, 0) + 1
        rec["emotion_dist"][m.emotion] = rec["emotion_dist"].get(m.emotion, 0) + 1
        if m.visual.sticker:
            rec["sticker_hits"] += 1
        if m.visual.overlay_sticker:
            rec["overlay_hits"] += 1
        if m.visual.sfx:
            rec["sfx_hits"] += 1

    lib_dir = _library_dir()
    # 追加到 library_index.json（不覆盖历史）
    index_path = os.path.join(lib_dir, "library_index.json")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
    except Exception:
        index = []

    # 去重：同名项目只保留最新一条
    index = [r for r in index if r.get("project_name") != dialogue.project_name]
    index.append(rec)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    # 单独存一份可读记录
    rec_path = os.path.join(lib_dir, f"{dialogue.project_name}.json")
    with open(rec_path, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    return rec_path


def list_library() -> List[dict]:
    """列出已归档项目（从 index 读取）。"""
    index_path = os.path.join(_library_dir(), "library_index.json")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []
