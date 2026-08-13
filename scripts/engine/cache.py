# -*- coding: utf-8 -*-
"""缓存层（v5 架构）。

按内容 hash 缓存昂贵产物，重复生成时直接复用：
  - TTS 音频：hash(text + speaker_id) -> mp3
  - 变速音频：hash(原始路径 + factor)
  - HTML 场景：hash(dialogue 摘要 + 时序)
  - 素材匹配：hash(emotion + 素材类型)

默认缓存根：<project>/assets/cache/
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Optional


def content_hash(*parts) -> str:
    """对多个字符串/字节片段做 md5。"""
    h = hashlib.md5()
    for p in parts:
        if isinstance(p, str):
            h.update(p.encode("utf-8"))
        elif isinstance(p, bytes):
            h.update(p)
        else:
            h.update(json.dumps(p, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return h.hexdigest()


class Cache:
    """本地磁盘缓存。线程安全不做保证（本工具单进程）。"""

    def __init__(self, root_dir: str = "", enabled: bool = True) -> None:
        self.root = os.path.abspath(root_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "cache"))
        self.enabled = enabled
        self._dirs = {
            "tts": os.path.join(self.root, "tts"),
            "speedup": os.path.join(self.root, "speedup"),
            "html": os.path.join(self.root, "html"),
            "assets": os.path.join(self.root, "assets"),
        }
        if enabled:
            for d in self._dirs.values():
                os.makedirs(d, exist_ok=True)

    # ---- 通用 ----
    def _path(self, kind: str, key: str, ext: str) -> str:
        return os.path.join(self._dirs.get(kind, self.root), f"{key}{ext}")

    def get(self, kind: str, key: str, ext: str) -> Optional[str]:
        """命中返回路径，未命中返回 None。"""
        if not self.enabled:
            return None
        p = self._path(kind, key, ext)
        return p if os.path.isfile(p) else None

    def put(self, kind: str, key: str, ext: str, src_path: str) -> str:
        """复制 src_path 到缓存并返回缓存路径。"""
        dst = self._path(kind, key, ext)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.abspath(src_path) != os.path.abspath(dst):
            shutil.copy2(src_path, dst)
        return dst

    # ---- TTS ----
    def tts_key(self, text: str, speaker: str) -> str:
        return content_hash(text, speaker)

    def get_tts(self, text: str, speaker: str) -> Optional[str]:
        return self.get("tts", self.tts_key(text, speaker), ".mp3")

    def put_tts(self, text: str, speaker: str, src_path: str) -> str:
        return self.put("tts", self.tts_key(text, speaker), ".mp3", src_path)

    # ---- 变速 ----
    def get_speedup(self, src_path: str, factor: float) -> Optional[str]:
        return self.get("speedup", content_hash(os.path.abspath(src_path), factor), ".mp3")

    def put_speedup(self, src_path: str, factor: float, dst: str) -> str:
        return self.put("speedup", content_hash(os.path.abspath(src_path), factor), ".mp3", dst)

    # ---- HTML ----
    def get_html(self, scene_key: str) -> Optional[str]:
        return self.get("html", scene_key, ".html")

    def put_html(self, scene_key: str, html: str) -> str:
        """直接写 HTML 文本到缓存。返回路径。"""
        if not self.enabled:
            return ""
        p = self._path("html", scene_key, ".html")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(html)
        return p

    # ---- 素材匹配 ----
    def get_asset(self, kind: str, emotion: str) -> Optional[str]:
        return self.get("assets", content_hash(kind, emotion), ".json")

    def put_asset(self, kind: str, emotion: str, asset: dict) -> str:
        p = self._path("assets", content_hash(kind, emotion), ".json")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(asset, f, ensure_ascii=False, indent=2)
        return p

    # ---- 清理 ----
    def clear(self, kind: str = "") -> None:
        if kind:
            d = self._dirs.get(kind)
            if d and os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
                os.makedirs(d, exist_ok=True)
        else:
            for d in self._dirs.values():
                if os.path.isdir(d):
                    shutil.rmtree(d, ignore_errors=True)
            for d in self._dirs.values():
                os.makedirs(d, exist_ok=True)


# 全局单例
CACHE = Cache()
