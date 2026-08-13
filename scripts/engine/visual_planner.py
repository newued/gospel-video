# -*- coding: utf-8 -*-
"""视觉规划：Dialogue -> 贴纸/音效/动画/转场（v5 架构 visual_planner 模块）。

从 gospel_automator 阶段1（build_plan 的素材匹配段）提炼并迁移而来，
把决策逻辑从 automator 收拢到独立模块，Renderer 只执行不决策。

规则：
  - side：首个非旁白角色 left，第二个 right，旁白 center
  - 贴纸（正式内容）：剧本显式 image/sticker 路径存在直接用；否则走
    AssetProvider.find_sticker(emotion)；旁白不加贴纸；匹配不到记 missing
  - overlay_sticker（普通 text 消息的下半屏覆盖贴纸）：按情绪匹配，静默跳过
  - SFX 克制：剧本显式必配；强喜剧标记 / 强情绪(angry|surprise)+情绪词 / 强标点+
    情绪词 / 短感叹句才配；同人连续上条已配则跳过；匹配不到记 missing
  - 入场动画/转场/字幕动画：动画名写入 visual 字段（Renderer 只执行）
  - 缺失清单统一写入 dialogue.meta["missing_assets"]
"""
from __future__ import annotations

import os
import random
from typing import Optional

from .assets import Registry, get_registry
from .models import Asset, Dialogue
from .effects_catalog import normalize_effect, category_of

# 素材积分常量（与 gospel_automator 保持一致）
ASSET_POINT = 1

# 旁白角色别名（与 automator 的 is_narration 判定一致）
NARRATION_ROLES = ("旁白", "narrator", "解说", "画外音")

# 消息入场动画（与 chat_scene_renderer 的 MSG_ENTRY_ANIMS 保持一致）
MSG_ENTRY_ANIMS = [
    "msgPop", "msgSlideUp", "msgSlideLeft", "msgSlideRight",
    "msgZoom", "msgBounce", "msgSwing", "msgDrop", "msgFlip",
]

# 强喜剧标记：出现即配音效
STRONG_COMEDY_MARKERS = (
    "哈哈哈", "哈哈哈哈", "卧槽", "我靠", "绝了", "麻了", "离谱",
     "破防", "无语", "笑死", "666", "牛逼", "牛批",
    "？？？", "！！！", "啊？", "哦？", "啥？", "真的假的",
)

# 情绪关键词（与 automator 的 emotion_keywords 保持一致）
EMOTION_KEYWORDS = {
    "angry": ("生气", "气死", "愤怒", "放屁", "扯淡", "狗屁", "滚", "闭嘴", "烦"),
    "surprise": ("震惊", "惊呆", "不敢相信", "我的天", "天哪", "哇", "我去", "离谱"),
    "happy": ("哈哈", "笑死", "优秀", "牛皮", "666", "可以可以"),
    "sad": ("哭", "难过", "委屈", "崩溃", "麻了", "emo", "心累"),
}


def _is_narration(msg) -> bool:
    """判断消息是否旁白。"""
    return bool(msg.narration) or msg.role in NARRATION_ROLES


def _explicit_sticker(msg) -> str:
    """剧本显式声明的贴纸路径（image / sticker 字段）。"""
    return msg.image or getattr(msg, "sticker", "") or ""


def _record_missing(missing: list, kind: str, index: int, emotion: str,
                    detail: str, hint: str) -> None:
    """记录一条缺失素材，供人工补齐。"""
    missing.append({
        "type": kind, "index": index, "emotion": emotion,
        "detail": detail, "hint": hint,
    })


def _resolve_formal_sticker(msg, index: int, registry: Registry,
                            missing: list) -> Optional[Asset]:
    """正式内容贴纸：显式路径存在直接用；否则按情绪匹配；匹配不到记 missing。"""
    emotion = msg.emotion or "neutral"
    explicit = _explicit_sticker(msg)
    if explicit:
        # 相对路径统一按项目根解析（兼容 scripts/ 与项目根两种 cwd）
        if not os.path.isabs(explicit):
            from random_assets import PROJECT_ROOT
            explicit = os.path.join(PROJECT_ROOT, explicit)
        if os.path.exists(explicit):
            return Asset(type="sticker", path=explicit, name=os.path.basename(explicit),
                         emotion=emotion, provider="manual")
        _record_missing(missing, "sticker", index, emotion,
                        f"剧本指定贴纸文件不存在: {explicit}",
                        "请修正剧本中的图片路径，或人工在剪映中插入该贴纸")
        return None
    asset = registry.find_sticker(emotion)
    if asset:
        return asset
    _record_missing(missing, "sticker", index, emotion,
                    "assets/emojis/ 中没有匹配该情绪的贴纸",
                    "请向 assets/emojis/ 补充素材，或人工在剪映中插入")
    return None


def _plan_sticker(msg, index: int, registry: Registry, missing: list) -> None:
    """回填 sticker / overlay_sticker（含贴纸定位规则）。"""
    v = msg.visual
    is_narration = _is_narration(msg)
    # 贴纸消息 / 显式声明贴纸 = 正式对话内容，强制匹配（不凑合）
    if msg.type == "sticker" or _explicit_sticker(msg):
        asset = _resolve_formal_sticker(msg, index, registry, missing)
        v.sticker = asset.path if asset else ""
        v.sticker_info = {"path": asset.path, "provider": asset.provider} if asset else {}
        v.overlay_sticker = ""
        v.overlay_sticker_info = {}
    elif is_narration:
        # 旁白不加贴纸（居中显示，贴纸会破坏画面）
        v.sticker = ""
        v.sticker_info = {}
        v.overlay_sticker = ""
        v.overlay_sticker_info = {}
    else:
        # 普通 text 消息 = 覆盖贴纸（情绪补充，匹配不到静默跳过）
        v.sticker = ""
        v.sticker_info = {}
        asset = registry.find_sticker(msg.emotion or "neutral")
        if asset:
            v.overlay_sticker = asset.path
            v.overlay_sticker_info = {"path": asset.path, "provider": asset.provider}
        else:
            v.overlay_sticker = ""
            v.overlay_sticker_info = {}


def _plan_sfx(msg, index: int, registry: Registry, missing: list,
              prev_sfx_used: bool) -> None:
    """克制配音效：显式必配；强喜剧/强情绪才配；同人连续上条已配则跳过。"""
    v = msg.visual
    # 剧本显式 sfx（上游已写入 visual.sfx.effect_id）→ 必配，直接保留
    if v.sfx.get("effect_id"):
        return
    text = (msg.text or "").strip()
    if not text:
        return
    emotion = msg.emotion or "neutral"

    # 强喜剧标记直接通过
    has_strong_comedy = any(m in text for m in STRONG_COMEDY_MARKERS)

    # 强标点：感叹号/问号多
    exclaim_count = text.count("！") + text.count("!")
    question_count = text.count("？") + text.count("?")
    has_strong_punct = (exclaim_count >= 2 or question_count >= 2
                        or (exclaim_count >= 1 and question_count >= 1))

    # 情绪关键词
    has_emotion_kw = any(kw in text for kws in EMOTION_KEYWORDS.values() for kw in kws)

    # 判定是否需要音效（严格克制）
    need_sfx = False
    reason = ""
    if has_strong_comedy:
        need_sfx = True
        reason = "strong_comedy"
    elif emotion in ("angry", "surprise") and (has_emotion_kw or has_strong_punct):
        need_sfx = True
        reason = f"strong_emotion:{emotion}"
    elif has_strong_punct and has_emotion_kw:
        need_sfx = True
        reason = "punct+emotion"
    elif len(text) <= 10 and text[-1] in "！!" and has_emotion_kw:
        need_sfx = True
        reason = "short_exclaim"
    else:
        need_sfx = False

    if not need_sfx:
        return
    # 同人连续说话且上一条已配 → 跳过，避免连续轰炸
    if prev_sfx_used:
        return

    asset = registry.find_sfx(emotion)
    if not asset:
        _record_missing(missing, "sfx", index, emotion,
                        f"音效库中未找到匹配该情绪的音效（触发原因: {reason}）",
                        "请人工在剪映音频素材库中挑选，并把 effect_id 填回 plan.json 的 sfx.effect_id")
        return
    v.sfx = {
        "effect_id": asset.meta.get("effect_id", ""),
        "title": asset.name,
        "duration_s": asset.meta.get("duration_s", ""),
        "source": "auto",
        "reason": reason,
    }


def _plan_scene_effect(msg, index: int, missing: list) -> None:
    """画面特效：消费 msg.effects 标记，归一化为剪映 identifier 写入 visual.scene_effect。

    - 一条消息上可挂多个特效标记，取第一个有效特效（剪映单 message 段挂一个画面特效即可；
      多特效需求可通过在不同消息上分别标记实现）。
    - 归一化失败（剪映无此模板）不阻断，仅在 missing 清单提示，由 QA 闸门汇总报告。
    """
    v = msg.visual
    effs = msg.effects
    if not effs:
        return
    # 取第一个有效标记
    chosen = None
    for e in effs:
        name = (e.get("name") or "").strip()
        if name:
            chosen = e
            break
    if not chosen:
        return
    raw_name = chosen["name"]
    norm = normalize_effect(raw_name)
    v.scene_effect = norm
    # 归一化后仍可在 jianying video_scene_effects.csv 枚举命中；若明显不匹配则记录
    cat = category_of(norm)
    if cat == "未知" and norm == raw_name:
        # 既不在归一化表、也不在已知分类 -> 交给 _resolve_enum 本地匹配，这里仅轻提示
        _record_missing(missing, "scene_effect", index, msg.emotion or "neutral",
                        f"画面特效「{raw_name}」未在内置对照表/分类中，将尝试按原名在剪映素材库匹配",
                        "若剪映无此模板，请改用在 video_scene_effects.csv 中存在的相近特效名")
    # 标记已消费，清空避免重复
    msg.effects = None


def _plan_animations(msg, registry: Registry) -> None:
    """回填转场 / 字幕文字动画（动画名存 visual 字段，Renderer 只执行）。"""
    v = msg.visual
    trans = registry.find_transition()
    if trans:
        v.transition = trans.name
    text_anim = registry.find_text_animation()
    if text_anim:
        v.text_animation = text_anim.name


def plan_visual(dialogue: Dialogue, registry: Optional[Registry] = None) -> Dialogue:
    """回填每条 Message.visual：side/sticker/overlay_sticker/sfx/animation/transition/text_animation。

    缺失素材清单写入 dialogue.meta["missing_assets"]。
    """
    registry = registry or get_registry()
    missing: list = list(dialogue.meta.get("missing_assets", []))

    # 左右角色分配：首个非旁白角色 left，第二个 right
    roles_order = []
    for m in dialogue.messages:
        if not _is_narration(m) and m.role and m.role not in roles_order:
            roles_order.append(m.role)

    def _side_of(role: str) -> str:
        if role in roles_order:
            return "left" if roles_order.index(role) % 2 == 0 else "right"
        return "left"

    last_anim = None
    prev_sfx_used = False  # 同人连续说话时，上一条已配则本条跳过
    for i, msg in enumerate(dialogue.messages):
        is_narration = _is_narration(msg)
        v = msg.visual

        # ---- 画面特效（消费 [[特效:...]] 标记，归一化为剪映 identifier）----
        _plan_scene_effect(msg, i, missing)

        # ---- 左右位置 ----
        v.side = "center" if is_narration else _side_of(msg.role)

        # ---- 入场动画（旁白固定上滑，其余随机且不连续重复）----
        if is_narration:
            v.animation = "msgSlideUp"
        else:
            anim = random.choice(MSG_ENTRY_ANIMS)
            if anim == last_anim and len(MSG_ENTRY_ANIMS) > 1:
                anim = random.choice([a for a in MSG_ENTRY_ANIMS if a != last_anim])
            v.animation = anim
        last_anim = v.animation

        # ---- 贴纸 / 覆盖贴纸 ----
        _plan_sticker(msg, i, registry, missing)

        # ---- 音效（克制规则）----
        _plan_sfx(msg, i, registry, missing, prev_sfx_used)
        prev_sfx_used = bool(v.sfx) if (i > 0 and msg.role == dialogue.messages[i - 1].role) else False

        # ---- 转场 / 字幕动画 ----
        _plan_animations(msg, registry)

    # 缺失清单 + 积分估算写入 meta
    dialogue.meta["missing_assets"] = missing
    sfx_count = sum(1 for m in dialogue.messages if m.visual.sfx)
    sticker_count = sum(1 for m in dialogue.messages if m.visual.sticker)
    overlay_count = sum(1 for m in dialogue.messages if m.visual.overlay_sticker)
    dialogue.meta["asset_points_used"] = (sfx_count + sticker_count + overlay_count) * ASSET_POINT
    return dialogue
