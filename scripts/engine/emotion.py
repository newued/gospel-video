# -*- coding: utf-8 -*-
"""情绪检测器：text -> emotion。

独立可替换模块（换情绪模型时无需改动其他模块）。
"""
from __future__ import annotations

from typing import Dict, List


# 情绪关键词表（关键词匹配优先于标点判断）
EMOTION_KEYWORDS: Dict[str, List[str]] = {
    "angry": ["生气", "气死", "愤怒", "放屁", "扯淡", "狗屁", "滚", "闭嘴", "烦",
              "什么鬼", "卧槽", "我靠", "岂有此理", "离谱", "麻了", "气死", "可恶"],
    "surprise": ["震惊", "惊呆", "不敢相信", "我的天", "天哪", "哇", "我去",
                 "啊？", "哦？", "啥？", "真的假的", "什么", "？", "？！"],
    "happy": ["哈哈", "笑死", "优秀", "牛皮", "666", "可以可以", "佩服", "绝了",
              "太棒了", "好耶", "嘿嘿", "呵呵", "厉害", "佩服佩服", "牛"],
    "sad": ["哭", "难过", "委屈", "崩溃", "emo", "心累", "唉", "哎",
            "生意不好", "不行了", "完了", "惨", "穷"],
}


def detect_emotion(text: str) -> str:
    """根据文本内容自动判断情绪。

    优先级：关键词匹配 > 强标点（？？/！！）> 疑问词 + 问号 > neutral。
    返回值：angry | surprise | happy | sad | neutral
    """
    text = text.strip()
    if not text:
        return "neutral"
    # 先做关键词匹配（优先级最高）
    for emo, kws in EMOTION_KEYWORDS.items():
        for kw in kws:
            if kw in text:
                return emo
    # 强标点标记
    if "？？" in text or "？！" in text or "!?" in text or "??" in text:
        return "surprise"
    if "！！" in text or "!!" in text:
        return "happy"
    # 单个问号但有疑问词
    if (text.endswith("？") or text.endswith("?")):
        if any(k in text for k in ["怎么", "为什么", "为啥", "难道", "该怎么办", "什么意思"]):
            return "surprise"
    return "neutral"
