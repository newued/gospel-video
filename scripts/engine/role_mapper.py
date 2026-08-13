# -*- coding: utf-8 -*-
"""角色映射器：RawMsg 标签 -> 角色名 + 音色 speaker_id。

职责：
  - 角色自动命名（年轻人/大师/师傅...，旁白固定"旁白"）
  - 音色关键词 -> speaker_id 映射（VOICE_KEYWORDS）
  - 字母标签按出现顺序分配默认角色名与默认音色
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Dict, List, Optional, Set, Tuple

from .emotion import detect_emotion
from .models import AudioClip, Message

if TYPE_CHECKING:
    from .parser import RawMsg


# 角色音色自动映射（关键词 -> speaker_id），优先级从高到低
VOICE_KEYWORDS: List[Tuple[str, str]] = [
    # 中年/沉稳/大叔（优先级高，先匹配）
    ("中年", "BV701_streaming"), ("沉稳", "BV701_streaming"), ("大叔", "BV107_streaming"),
    ("成熟", "zh_male_vopyounan"), ("厚实", "ICL_zh_male_houzhongzhengqi"),
    ("老道", "BV701_streaming"), ("深沉", "zh_male_iclvop_miaojijsqinggan"),
    # 旁白/广播
    ("旁白", "zh_male_voplvyou"), ("广播", "zh_male_voplvyou"), ("播音", "zh_male_voplvyou"),
    ("新闻", "BV002_streaming"), ("解说", "BV701_streaming"), ("讲述", "zh_male_rendongteng"),
    # 年轻男生
    ("年轻", "BV056_streaming"), ("干净", "BV056_streaming"), ("阳光", "BV056_streaming"),
    ("活力", "zh_male_huoli"), ("清亮", "zh_male_iclvop_xiaolinkepu"),
    ("少年", "zh_male_shaonianzixin_moon_bigtts"),
    # 其他
    ("磁性", "zh_male_iclvop_zhangjinxiangnanzhu"), ("温柔", "zh_male_iclvop_chenxuanqinggan"),
]

# 角色自动命名（按出现顺序分配，A=第一个说话的角色，B=第二个...）
DEFAULT_ROLE_NAMES: List[str] = ["年轻人", "大师", "师傅", "老王", "小张", "小李", "老板", "客户"]

# 旁白角色名
NARRATOR_ROLES: Set[str] = {"旁白", "narrator", "解说", "画外音", "Narrator"}

# 字母标签按出现顺序分配的默认音色（idx0 阳光男生，idx1 沉稳解说，其余磁性男声）
_ORDERED_DEFAULT_SPEAKERS: List[str] = [
    "BV056_streaming",  # 阳光男生
    "BV701_streaming",  # 沉稳解说
    "zh_male_iclvop_zhangjinxiangnanzhu",  # 磁性男声
]

# 旁白固定默认音色（播音腔）
_NARRATOR_DEFAULT_SPEAKER: str = "zh_male_voplvyou"


def _infer_speaker(
    label: str,
    role: str,
    hints: Dict[str, str],
    label_order: List[str],
    default_speaker: str,
) -> str:
    """推断某个角色标签的音色 speaker_id。

    优先级：音色建议 hints > 关键词推断 > 旁白默认 > 按出现顺序默认 > default_speaker。
    """
    # 1. 音色建议行优先
    if label in hints:
        return hints[label]
    if role in hints:
        return hints[role]
    # 2. 关键词推断（在角色名/标签中匹配音色关键词）
    for kw, sp in VOICE_KEYWORDS:
        if kw in role or kw in label:
            return sp
    # 3. 旁白固定默认播音腔
    if role == "旁白":
        return _NARRATOR_DEFAULT_SPEAKER
    # 4. 字母标签按出现顺序分配默认音色
    if label in label_order:
        idx = label_order.index(label)
        if idx < len(_ORDERED_DEFAULT_SPEAKERS):
            return _ORDERED_DEFAULT_SPEAKERS[idx]
    # 5. 兜底
    return default_speaker


def map_roles(
    raws: "List[RawMsg]",
    hints: Optional[Dict[str, str]] = None,
    default_speaker: str = "zh_male_huoli",
) -> Tuple[List[Message], Dict[str, str]]:
    """将 RawMsg 列表映射为带 role/emotion/voice 的 Message 列表。

    规则：
      - 字母标签按出现顺序分配 DEFAULT_ROLE_NAMES
      - 旁白角色固定命名"旁白"
      - 中文标签直接用其本身作为角色名
      - 音色：hints 里标签优先 > 关键词推断 > 顺序默认 > default_speaker

    返回：
      - messages: 已分配 role/emotion 的 Message 列表（id 从 0 递增，speaker 存原始标签）
      - role_speakers: {角色名: speaker_id}
    """
    hints = hints or {}
    messages: List[Message] = []
    role_speakers: Dict[str, str] = {}
    role_label_map: Dict[str, str] = {}  # 字母标签 -> 角色名（记录出现顺序）
    label_order: List[str] = []

    for raw in raws:
        label = raw.speaker

        # 1. 决定角色名
        if label in NARRATOR_ROLES:
            role = "旁白"
        elif re.fullmatch(r"[\u4e00-\u9fa5]+", label):
            # 中文标签（如"大师"）直接用其本身作为角色名
            role = label
        elif label not in role_label_map:
            # 字母标签：按出现顺序分配默认角色名
            used = set(role_label_map.values())
            role = next(
                (n for n in DEFAULT_ROLE_NAMES if n not in used),
                f"角色{len(used) + 1}",
            )
            role_label_map[label] = role
            label_order.append(label)
        else:
            role = role_label_map[label]

        # 2. 决定音色
        speaker_id = _infer_speaker(label, role, hints, label_order, default_speaker)
        if role not in role_speakers:
            role_speakers[role] = speaker_id

        # 3. 构造 Message
        messages.append(Message(
            id=len(messages),
            speaker=label,
            role=role,
            text=raw.text,
            type="text",
            emotion=detect_emotion(raw.text),
            narration=raw.narration,
            effects=raw.effects,
            audio=AudioClip(voice=speaker_id),
        ))

    # 确保旁白音色存在（若有旁白消息但未在 hints/关键词中命中）
    if "旁白" not in role_speakers and any(m.narration for m in messages):
        role_speakers["旁白"] = _NARRATOR_DEFAULT_SPEAKER

    return messages, role_speakers
