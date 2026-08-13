# -*- coding: utf-8 -*-
"""素材提供者插件化层（v5 架构 assets 模块）。

AssetProvider 抽象出素材查询接口，各视觉阶段只依赖该接口，不再直接调用
random_assets.py 的底层函数。Registry 支持注册多个 Provider 并顺序查询，
第一个命中者返回；全部未命中返回 None（不凑合）。

内置实现 BuiltinProvider 封装 random_assets.py：
  pick_sticker           -> find_sticker
  sfx_for_emotion        -> find_sfx
  random_transition      -> find_transition
  random_text_animation  -> find_text_animation

产出统一为 engine.models.Asset 对象：
  type     sticker | sfx | transition | text_animation | avatar | bgm
  path     素材绝对路径（云素材如音效无本地文件时为空）
  name     音效标题 / 转场名 / 文字动画名
  meta     {effect_id, duration_s, ...} 扩展信息
  provider 来源标识（builtin / emoji / gif / custom）
"""
from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from typing import List, Optional

from .models import Asset

# scripts/ 目录加入 sys.path，使 random_assets.py 可被独立导入
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from random_assets import (  # noqa: E402
    pick_sticker,
    random_text_animation,
    random_transition,
    sfx_for_emotion,
)


class AssetProvider(ABC):
    """素材提供者抽象接口。找不到一律返回 None（不凑合）。"""

    @abstractmethod
    def find_sticker(self, emotion: str) -> Optional[Asset]:
        """按情绪匹配贴纸素材。"""

    @abstractmethod
    def find_sfx(self, emotion: str) -> Optional[Asset]:
        """按情绪匹配音效素材。"""

    @abstractmethod
    def find_transition(self) -> Asset:
        """随机挑选转场。"""

    @abstractmethod
    def find_text_animation(self) -> Asset:
        """随机挑选字幕文字动画。"""

    @abstractmethod
    def find_avatar(self, role: str) -> Optional[Asset]:
        """按角色挑选头像素材（无来源返回 None）。"""

    @abstractmethod
    def find_bgm(self, query: str) -> Optional[Asset]:
        """按关键词挑选 BGM 素材（无来源返回 None）。"""


class BuiltinProvider(AssetProvider):
    """内置提供者：封装 random_assets.py 的本地素材库选择器。"""

    provider_name = "builtin"

    def find_sticker(self, emotion: str) -> Optional[Asset]:
        """本地表情包库按情绪匹配，找不到返回 None。"""
        path = pick_sticker(emotion)
        if not path:
            return None
        return Asset(
            type="sticker",
            path=path,
            name=os.path.basename(path),
            emotion=emotion,
            provider=self.provider_name,
        )

    def find_sfx(self, emotion: str) -> Optional[Asset]:
        """云音效库按情绪匹配（effect_id 模式，无本地文件）。"""
        sfx = sfx_for_emotion(emotion)
        if not sfx:
            return None
        return Asset(
            type="sfx",
            path="",
            name=sfx.get("title", ""),
            emotion=emotion,
            meta={
                "effect_id": sfx.get("effect_id", ""),
                "title": sfx.get("title", ""),
                "duration_s": sfx.get("duration_s", ""),
            },
            provider=self.provider_name,
        )

    def find_transition(self) -> Asset:
        """随机转场名（剪映 transitions.csv 的 identifier）。"""
        return Asset(
            type="transition",
            path="",
            name=random_transition(),
            provider=self.provider_name,
        )

    def find_text_animation(self) -> Asset:
        """随机字幕文字动画名（剪映 text_animations.csv 的 identifier）。"""
        return Asset(
            type="text_animation",
            path="",
            name=random_text_animation(),
            provider=self.provider_name,
        )

    def find_avatar(self, role: str) -> Optional[Asset]:
        """内置无头像素材来源，返回 None（由 Renderer 用首字+配色兜底）。"""
        return None

    def find_bgm(self, query: str) -> Optional[Asset]:
        """内置无 BGM 素材来源，返回 None（BGM 走外部 API）。"""
        return None


class Registry:
    """多 Provider 注册表：顺序查询，第一个命中的 provider 结果生效。"""

    def __init__(self) -> None:
        self._providers: List[AssetProvider] = []

    def add(self, provider: AssetProvider) -> None:
        """追加一个 provider（追加到默认 provider 之后）。"""
        if provider not in self._providers:
            self._providers.append(provider)

    def set_default(self, provider: AssetProvider) -> None:
        """设置默认 provider（查询时最先被询问）。"""
        if provider not in self._providers:
            self._providers.insert(0, provider)
        else:
            self._providers.remove(provider)
            self._providers.insert(0, provider)

    def _first_hit(self, fn) -> Optional[Asset]:
        """顺序询问各 provider，返回第一个非 None 结果。"""
        for p in self._providers:
            try:
                asset = fn(p)
            except Exception:
                # 单个 provider 出错不阻断流水线
                asset = None
            if asset is not None:
                return asset
        return None

    def find_sticker(self, emotion: str) -> Optional[Asset]:
        return self._first_hit(lambda p: p.find_sticker(emotion))

    def find_sfx(self, emotion: str) -> Optional[Asset]:
        return self._first_hit(lambda p: p.find_sfx(emotion))

    def find_transition(self) -> Optional[Asset]:
        return self._first_hit(lambda p: p.find_transition())

    def find_text_animation(self) -> Optional[Asset]:
        return self._first_hit(lambda p: p.find_text_animation())

    def find_avatar(self, role: str) -> Optional[Asset]:
        return self._first_hit(lambda p: p.find_avatar(role))

    def find_bgm(self, query: str) -> Optional[Asset]:
        return self._first_hit(lambda p: p.find_bgm(query))

    def find_any(self, kind: str, **kw) -> Optional[Asset]:
        """通用分发：kind = sticker|sfx|transition|text_animation|avatar|bgm。"""
        dispatch = {
            "sticker": lambda: self.find_sticker(kw.get("emotion", "neutral")),
            "sfx": lambda: self.find_sfx(kw.get("emotion", "neutral")),
            "transition": lambda: self.find_transition(),
            "text_animation": lambda: self.find_text_animation(),
            "avatar": lambda: self.find_avatar(kw.get("role", "")),
            "bgm": lambda: self.find_bgm(kw.get("query", "")),
        }
        fn = dispatch.get(kind)
        return fn() if fn else None


# 全局单例：默认注册内置提供者
_REGISTRY = None


def get_registry() -> Registry:
    """返回全局 Registry 单例（首次调用时注册 BuiltinProvider）。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = Registry()
        _REGISTRY.set_default(BuiltinProvider())
    return _REGISTRY
