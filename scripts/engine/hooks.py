# -*- coding: utf-8 -*-
"""Hook 系统（v5 架构）。

外部插件可在流水线关键节点注册回调，无需修改核心代码：
  before_parse / after_parse
  before_role_map / after_role_map
  before_emotion / after_emotion
  before_audio / after_audio
  before_alignment / after_alignment
  before_visual / after_visual
  before_timeline / after_timeline
  before_render / after_render
  before_assemble / after_assemble

用法：
  from engine.hooks import HOOKS
  HOOKS.register("after_timeline", my_fn)   # my_fn(ctx) -> ctx
  或一次性注册插件类：HOOKS.register_module(MyPlugin)
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional


# 全部 Hook 点位（顺序即执行顺序）
HOOK_POINTS: List[str] = [
    "before_parse", "after_parse",
    "before_role_map", "after_role_map",
    "before_emotion", "after_emotion",
    "before_audio", "after_audio",
    "before_alignment", "after_alignment",
    "before_visual", "after_visual",
    "before_timeline", "after_timeline",
    "before_render", "after_render",
    "before_assemble", "after_assemble",
]

# 回调签名：fn(ctx: PipelineContext) -> PipelineContext
HookFn = Callable[["object"], "object"]


class HookRegistry:
    """Hook 注册表：名字 -> 有序回调列表。"""

    def __init__(self) -> None:
        self._handlers: Dict[str, List[HookFn]] = {p: [] for p in HOOK_POINTS}

    def register(self, point: str, fn: HookFn, name: str = "") -> None:
        """注册一个回调到指定点位。name 仅用于调试。"""
        if point not in self._handlers:
            raise KeyError(f"未知 Hook 点位: {point}（可用: {HOOK_POINTS}）")
        fn.__hook_name__ = name or getattr(fn, "__name__", str(fn))
        self._handlers[point].append(fn)

    def register_module(self, plugin) -> int:
        """注册一个插件对象/模块：自动挂接其上所有 <point> 方法。
        返回挂接数量。方法名匹配 Hook 点位，如 after_timeline(ctx)。"""
        count = 0
        for point in HOOK_POINTS:
            fn = getattr(plugin, point, None)
            if callable(fn):
                self.register(point, fn, name=type(plugin).__name__)
                count += 1
        return count

    def run(self, point: str, ctx) -> object:
        """执行指定点位所有回调，返回（可能被改写的）ctx。"""
        if point not in self._handlers:
            return ctx
        for fn in self._handlers[point]:
            try:
                ctx = fn(ctx)
            except Exception as e:  # 插件异常不阻断主流程
                ctx.errors.append(f"[hook:{point}:{getattr(fn, '__hook_name__', '?')}] {e}")
        return ctx

    def clear(self, point: Optional[str] = None) -> None:
        if point:
            self._handlers[point] = []
        else:
            for p in self._handlers:
                self._handlers[p] = []

    def list(self, point: str = "") -> List[str]:
        if point:
            return [getattr(fn, "__hook_name__", "?") for fn in self._handlers.get(point, [])]
        return {
            p: [getattr(fn, "__hook_name__", "?") for fn in fns]
            for p, fns in self._handlers.items()
        }  # type: ignore


# 全局单例：所有模块共享
HOOKS = HookRegistry()
