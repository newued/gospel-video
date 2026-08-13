# -*- coding: utf-8 -*-
"""纯文本解析器：原始对话文本 -> RawMsg 结构列表。

只负责解析文本结构（[A]xxx / A: xxx / 大师：xxx / 续行归属 / 音色建议行剥离 /
旁白标签判定），不做角色命名、情绪判断、音色推断（分别交给 role_mapper / emotion）。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from .role_mapper import NARRATOR_ROLES, VOICE_KEYWORDS
from .effects_catalog import parse_effect_tags  # 解析 [[特效:名@时间|时长]] 标记


@dataclass
class RawMsg:
    """一条原始消息（未规范化的角色标识）。"""
    speaker: str = ""       # 原始标签 A/B/C/大师（未规范化的角色标识）
    text: str = ""          # 消息文本（已剥离 [[特效:...]] 标记）
    narration: bool = False  # 是否旁白
    effects: list = None    # 该条消息上挂载的画面特效 [{name, at, dur}]


# 格式1：[A] xxx / [旁白] xxx（支持一行内多条，如 "[A]你好 [B]怎么了"）
_RE_BRACKET_SPLIT = re.compile(r"\[([^\]]+)\]\s*([^\[]*)")
# 格式2：A: xxx / 大师：xxx（1-8 个字母或中文字符）
_RE_COLON = re.compile(r"^([A-Za-z\u4e00-\u9fa5]{1,8})[：:]\s*(.+)$")
# 音色建议行（整行剥离，不进入消息列表）
_RE_HINT_LINE = re.compile(r"^(?:音色建议|配音建议|声音|音色)[：:]")
# 音色建议行内容提取
_RE_HINT_BODY = re.compile(r"(?:音色建议|配音建议|声音|音色)[：:]\s*(.+)")
# 音色建议行内的标签定位（单字母或常见中文角色名）
_RE_HINT_LABEL = re.compile(r"([A-Za-z]|旁白|大师|年轻人|师傅|老师|老板|客户)")


def parse_text(raw_text: str) -> List[RawMsg]:
    """解析原始对话文本为 RawMsg 列表。

    支持格式：
      [A] xxx / [旁白] xxx（一行内可含多条，如 "[A]你好 [B]怎么了"）
      A: xxx / 大师：xxx
      无标记续行归属上一角色
      空行分隔（跳过）
    音色建议行（"音色建议：..."）被剥离，不进入消息列表。
    """
    raws: List[RawMsg] = []
    current_label: Optional[str] = None
    for line in raw_text.strip().split("\n"):
        line = line.strip()
        if not line or _RE_HINT_LINE.match(line):
            continue

        # 先把 [[特效:...]] 标记从整行剥离，避免被下方角色标签正则误判为 [标签]
        line_clean, line_effects = parse_effect_tags(line)

        speaker = None
        text = None
        # 优先按方括号标签切分（支持一行内多条：[A]你好 [B]怎么了）
        matches = list(_RE_BRACKET_SPLIT.finditer(line_clean))
        if matches:
            before = len(raws)
            for m in matches:
                sp = m.group(1).strip()
                tx = m.group(2).strip()
                if not sp or not tx:
                    continue
                current_label = sp
                raws.append(RawMsg(
                    speaker=sp,
                    text=tx,
                    narration=sp in NARRATOR_ROLES,
                    effects=None,
                ))
            # 行级特效挂到该行产出的第一个 RawMsg
            if line_effects and len(raws) > before:
                raws[before].effects = line_effects
            continue
        m2 = _RE_COLON.match(line_clean)
        if m2:
            speaker = m2.group(1).strip()
            text = m2.group(2).strip()
        elif current_label:
            # 无标记的续行，归属上一个角色
            speaker = current_label
            text = line_clean

        if not speaker or not text:
            continue
        current_label = speaker
        raws.append(RawMsg(
            speaker=speaker,
            text=text,
            narration=speaker in NARRATOR_ROLES,
            effects=line_effects or None,
        ))
    return raws


def parse_file(path: str) -> List[RawMsg]:
    """从文本文件读取并解析对话。

    若路径不存在，会尝试相对于 samples 目录查找（与旧 dialog_parser 行为一致）。
    """
    if not os.path.exists(path):
        # 本文件位于 scripts/engine/，向上三级即项目根目录
        samples_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "samples",
        )
        sample_path = os.path.join(samples_dir, path)
        if os.path.exists(sample_path):
            path = sample_path
        else:
            raise FileNotFoundError(f"文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return parse_text(f.read())


def extract_voice_hints(raw_text: str) -> Dict[str, str]:
    """解析"音色建议"行，返回 {标签: speaker_id}。

    简单策略：按标签切分，每个标签后紧跟的描述词匹配 VOICE_KEYWORDS。
    例："音色建议：A干净男生 B中年男人 旁白广播男音"
        -> {"A": "BV056_streaming", "B": "BV701_streaming", "旁白": "zh_male_voplvyou"}
    """
    result: Dict[str, str] = {}
    hint_match = _RE_HINT_BODY.search(raw_text)
    if not hint_match:
        return result
    hint = hint_match.group(1).strip()

    # 找出所有标签位置
    matches = list(_RE_HINT_LABEL.finditer(hint))
    for i, m in enumerate(matches):
        label = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(hint)
        desc = hint[start:end].strip()
        # 在描述中匹配音色关键词（VOICE_KEYWORDS 优先级从高到低）
        for kw, sp in VOICE_KEYWORDS:
            if kw in desc:
                result[label] = sp
                break
    return result


def infer_bgm_query(text: str) -> str:
    """从对话文本推断 BGM 关键词，多个关键词用空格连接。

    关键词规则：
      哈哈/笑死/搞笑 -> 搞笑
      悬疑/鬼/害怕/恐怖 -> 悬疑
      感动/哭/泪/温情 -> 温情
      帝王/霸气/佩服 -> 燃
    无匹配时返回默认 "轻松 搞笑"。
    """
    keywords = []
    if any(k in text for k in ("哈哈", "笑死", "搞笑")):
        keywords.append("搞笑")
    if any(k in text for k in ("悬疑", "鬼", "害怕", "恐怖")):
        keywords.append("悬疑")
    if any(k in text for k in ("感动", "哭", "泪", "温情")):
        keywords.append("温情")
    if any(k in text for k in ("帝王", "霸气", "佩服")):
        keywords.append("燃")
    return " ".join(keywords) if keywords else "轻松 搞笑"
