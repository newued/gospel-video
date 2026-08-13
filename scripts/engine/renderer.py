# -*- coding: utf-8 -*-
"""渲染抽象：Renderer 只执行不决策。

输入 Timeline（决策结果全部在 timeline 里），调 chat_scene_renderer 出视频。
本模块不做任何选择：动画名 / 贴纸 / 侧边 均来自 timeline 的 message 轨。
"""
import os

from engine.models import Timeline, Dialogue

# 渲染产物默认放在该目录（相对工作目录，可被上层覆盖）
DEFAULT_CHAT_VIDEO_REL = os.path.join("assets", "chat_video")


def render_chat(timeline: Timeline, dialogue: Dialogue, out_video: str,
                width: int = 1080, height: int = 1920,
                asset_dir: str = "") -> str:
    """按 timeline 渲染聊天视频，返回 out_video 路径；失败抛异常。

    - 从 timeline 的 message 轨提取逐条时序/动画/侧边/覆盖贴纸
    - 调 chat_scene_renderer.render_chat_scene 真正渲染
    - 决策（动画名/贴纸）已在 timeline 里，本模块不做任何选择
    """
    if timeline is None or dialogue is None:
        raise ValueError("render_chat: timeline 与 dialogue 均不能为空")

    # 从 timeline message 轨提取逐条消息的展示信息（顺序即对话顺序）
    msg_items = sorted(timeline.items_of("message"), key=lambda i: (i.start_s, i.text))
    script_messages = []
    msg_timings = []  # (index, start, end, side, animation, overlay_sticker)
    for it in msg_items:
        p = it.payload
        idx = p.get("message_index")
        msg = None
        for m in dialogue.messages:
            if m.id == idx:
                msg = m
                break
        role = p.get("role", "") or (msg.role if msg else "")
        text = p.get("text", "") or (msg.text if msg else "")
        emotion = p.get("emotion") or (msg.emotion if msg else "neutral")
        is_narration = bool(p.get("is_narration") or (msg.narration if msg else False))
        # 正式内容贴纸 -> image（大图消息）；下半屏贴纸 -> sticker
        image = p.get("sticker") or (msg.visual.sticker if msg else None)
        sticker = p.get("overlay_sticker") or (msg.visual.overlay_sticker if msg else None)
        script_messages.append({
            "role": role,
            "type": p.get("type") or (msg.type if msg else "text"),
            "text": text,
            "emotion": emotion,
            "is_narration": is_narration,
            "image": image,
            "sticker": sticker,
        })
        msg_timings.append((
            idx,
            round(it.start_s, 3),
            round(it.end_s, 3),
            p.get("side", "left"),
            p.get("animation") or "",
            sticker,
        ))

    # 组装 script 契约（chat_scene_renderer 消费的结构）
    script = {
        "title": timeline.title or dialogue.title or "",
        "messages": script_messages,
    }

    out_dir = os.path.dirname(out_video) or "."
    os.makedirs(out_dir, exist_ok=True)

    # 延迟导入：避免本模块顶层耦合渲染实现（gospel_automator 兼容层也会用）
    from chat_scene_renderer import render_chat_scene

    ok = render_chat_scene(
        script,
        output_video=out_video,
        width=width,
        height=height,
        msg_timings=msg_timings,
        speedup=timeline.speedup or 1.0,
        total_duration=timeline.total_duration_s,
        asset_dir=asset_dir or None,
    )
    if not ok:
        raise RuntimeError(f"render_chat: render_chat_scene 渲染失败 -> {out_video}")
    return out_video


def default_chat_video_path(project_name: str) -> str:
    """生成默认聊天视频输出路径（供 RenderNode 无配置时兜底）。"""
    name = (project_name or "gospel_video").strip() or "gospel_video"
    return os.path.join(DEFAULT_CHAT_VIDEO_REL, f"{name}_chat.webm")
