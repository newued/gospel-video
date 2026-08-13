# -*- coding: utf-8 -*-
"""时间轴规划器：把 Dialogue（各阶段结果）转成真正的时间轴 Timeline。

v5 架构契约（见 docs/ARCHITECTURE_v5.md）：
  message 轨   消息气泡（谁、何时出现、动画）
  audio 轨     配音（voice）+ 整曲（song）；BGM 保留在 meta（剪映组装用）
  subtitle 轨  字幕（原文 + 文字动画）
  effect 轨    贴纸（sticker）/ 音效（sfx）/ 转场（transition）

本模块只做"规划"（把已有结果落成时间轴），不做任何渲染/决策。
"""
import os
import json

from engine.models import Dialogue, Timeline, Track, TimelineItem

# 结尾留白（秒），与旧 gospel_automator.TAIL_PADDING_S 保持一致
TAIL_PADDING_S = 1.5
# 转场在时间轴上的建议时长（秒）
TRANSITION_DURATION_S = 0.8


def build_timeline(dialogue: Dialogue) -> Timeline:
    """把 Dialogue 变成 4 条轨道的时间轴。

    - message 轨：每条消息一条，payload 含 role/text/emotion/side/animation/
      sticker/overlay_sticker/exit_at 等决策结果（决策已在 visual_planner 做好）
    - audio 轨：逐句配音 kind=voice；整曲 kind=song（song_mode 时）；BGM 不建轨
    - subtitle 轨：每条消息字幕（原文 + 文字动画）
    - effect 轨：贴纸 / 音效 / 转场
    - total_duration_s = max(最后一条 end_s, 整曲时长) + TAIL_PADDING_S
    """
    if dialogue is None:
        raise ValueError("build_timeline: dialogue 不能为空")

    timeline = Timeline(
        schema_version=5,
        project_name=dialogue.project_name or "GospelVideo",
        title=dialogue.title or "",
        resolution=dialogue.resolution or 1080,
        total_duration_s=0.0,
        speedup=dialogue.speedup or 1.0,
        bgm_query=dialogue.bgm_query or "",
        song_mode=bool(dialogue.meta.get("song_mode")),
        song_audio=dialogue.meta.get("song_audio") or "",
        tracks=[],
        meta={},
    )

    msg_track = timeline.track("message")
    audio_track = timeline.track("audio")
    sub_track = timeline.track("subtitle")
    eff_track = timeline.track("effect")

    last_end = 0.0
    has_transition = False

    for m in dialogue.messages:
        start_s = float(m.audio.start_s or 0.0)
        end_s = float(m.audio.end_s or start_s)
        if end_s < start_s:
            end_s = start_s
        dur_s = max(0.0, end_s - start_s)
        last_end = max(last_end, end_s)

        vis = m.visual
        side = (vis.side or "left") if not m.narration else "center"
        animation = vis.animation or ("msgSlideUp" if m.narration else "")
        exit_at = float(getattr(vis, "exit_at", None) or end_s)
        sticker = vis.sticker or None
        overlay = vis.overlay_sticker or None
        sfx = vis.sfx or None

        # ---- message 轨 ----
        msg_track.items.append(TimelineItem(
            type="message", kind="message",
            start_s=start_s, end_s=end_s, duration_s=dur_s,
            text=m.role,
            payload={
                "message_index": m.id,
                "role": m.role,
                "text": m.text,
                "type": m.type,
                "emotion": m.emotion,
                "is_narration": bool(m.narration),
                "side": side,
                "animation": animation,
                "sticker": sticker,
                "overlay_sticker": overlay,
                "sticker_info": vis.sticker_info or {},
                "overlay_sticker_info": vis.overlay_sticker_info or {},
                "exit_at": exit_at,
                "scene_effect": vis.scene_effect or "",
            },
        ))

        # ---- audio 轨（逐句配音） ----
        if m.audio.path:
            audio_track.items.append(TimelineItem(
                type="audio", kind="voice",
                start_s=start_s, end_s=end_s, duration_s=dur_s,
                text=m.role,
                payload={
                    "message_index": m.id,
                    "path": m.audio.path,
                    "voice": m.audio.voice,
                    "speedup": m.audio.speedup or timeline.speedup,
                    "manual": bool(m.audio.manual),
                },
            ))

        # ---- subtitle 轨 ----
        sub_track.items.append(TimelineItem(
            type="subtitle", kind="text",
            start_s=start_s, end_s=end_s, duration_s=dur_s,
            text=m.text,
            payload={"text_animation": vis.text_animation or ""},
        ))

        # ---- effect 轨：贴纸 ----
        if sticker:
            eff_track.items.append(TimelineItem(
                type="effect", kind="sticker",
                start_s=start_s, end_s=end_s, duration_s=dur_s,
                text=m.role,
                payload={
                    "message_index": m.id,
                    "path": sticker,
                    "sticker_info": vis.sticker_info or {},
                    "overlay_sticker_info": vis.overlay_sticker_info or {},
                },
            ))

        # ---- effect 轨：音效 ----
        if sfx:
            eff_track.items.append(TimelineItem(
                type="effect", kind="sfx",
                start_s=start_s, end_s=end_s,
                duration_s=float(sfx.get("duration_s") or dur_s),
                text=m.role,
                payload={
                    "message_index": m.id,
                    "effect_id": sfx.get("effect_id"),
                    "title": sfx.get("title", ""),
                    "duration_s": float(sfx.get("duration_s") or dur_s),
                },
            ))

        # ---- effect 轨：转场（取第一条带转场的消息，只放一个） ----
        if not has_transition and vis.transition:
            has_transition = True
            eff_track.items.append(TimelineItem(
                type="effect", kind="transition",
                start_s=start_s, end_s=end_s,
                duration_s=TRANSITION_DURATION_S,
                text=m.role,
                payload={"transition": vis.transition, "message_index": m.id},
            ))

        # ---- effect 轨：画面特效（scene_effect，剪映 identifier）----
        # 仅在二维码标记 [[特效:...]] 归一化后有值。at/dur 由标记指定，缺省覆盖气泡段。
        se = (vis.scene_effect or "").strip()
        if se:
            from .effects_catalog import default_duration_for
            # 标记可能携带 at/dur（存在 m.effects 原标记里），但 visual_planner 已清空；
            # 这里兜底使用推荐默认时长，覆盖该气泡时间窗
            se_start = start_s
            se_dur = float(dur_s or default_duration_for(se))
            eff_track.items.append(TimelineItem(
                type="effect", kind="scene_effect",
                start_s=se_start, end_s=se_start + se_dur,
                duration_s=se_dur,
                text=m.role,
                payload={
                    "message_index": m.id,
                    "scene_effect": se,
                    "at": se_start,
                    "dur": se_dur,
                },
            ))

    # ---- audio 轨：整曲 song ----
    song_audio = timeline.song_audio
    song_dur = 0.0
    if song_audio:
        song_dur = float(dialogue.meta.get("total_duration_s") or last_end or 0.0)
        if song_dur <= 0:
            song_dur = last_end
        audio_track.items.append(TimelineItem(
            type="audio", kind="song",
            start_s=0.0, end_s=song_dur, duration_s=song_dur,
            text=timeline.title or "song",
            payload={"path": song_audio, "duration_s": song_dur},
        ))

    # ---- 总时长 ----
    if song_audio:
        # 整曲模式下视频不应超过歌曲实际时长 + 尾留白，避免画面超出音频（音画不同步）
        total = song_dur + TAIL_PADDING_S
    else:
        total = last_end + TAIL_PADDING_S
    timeline.total_duration_s = round(total, 3)

    # ---- meta 透传（供剪映组装 / 上层消费） ----
    _META_PASSTHROUGH = (
        "missing_assets", "song_mode", "bgm_query", "role_speakers",
        "speedup", "speaker", "manual_narrations", "asset_points_used",
        "alignment_method",
    )
    for key in _META_PASSTHROUGH:
        if key in dialogue.meta:
            timeline.meta[key] = dialogue.meta[key]
    timeline.meta["song_mode"] = bool(dialogue.meta.get("song_mode"))

    return timeline


def save_timeline(timeline: Timeline, path: str) -> str:
    """把 Timeline 序列化写入 path（UTF-8 JSON），返回 path。"""
    timeline.to_json(path)
    return path


def load_timeline(path: str) -> Timeline:
    """从 path 读取 Timeline（兼容 to_dict 序列化产物）。"""
    if not path or not os.path.isfile(path):
        raise FileNotFoundError(f"load_timeline: 找不到时间轴文件 {path}")
    with open(path, "r", encoding="utf-8") as f:
        return Timeline.from_dict(json.load(f))


def timeline_to_legacy_plan(timeline: Timeline) -> dict:
    """把 Timeline 转回旧 build_plan 的 plan dict 结构（schema_version=4）。

    供旧 gospel_automator._assemble_draft 无改动直接消费：
      messages 数组含 index/role/type/text/emotion/is_narration/
      start_s/end_s/duration_s/voice/sfx/sticker_path/sticker_info/
      overlay_sticker/overlay_sticker_info
    """
    if timeline is None:
        raise ValueError("timeline_to_legacy_plan: timeline 不能为空")

    msg_items = [it for it in timeline.items_of("message")]
    voice_items = {it.payload.get("message_index"): it
                   for it in timeline.items_of("audio") if it.kind == "voice"}
    sfx_items = {it.payload.get("message_index"): it
                 for it in timeline.items_of("effect") if it.kind == "sfx"}
    sticker_items = {it.payload.get("message_index"): it
                     for it in timeline.items_of("effect") if it.kind == "sticker"}

    messages = []
    for it in msg_items:
        p = it.payload
        idx = p.get("message_index", len(messages))
        voice_item = voice_items.get(idx)
        sfx_item = sfx_items.get(idx)
        sticker_item = sticker_items.get(idx)

        voice_path = None
        manual = None
        if voice_item:
            voice_path = voice_item.payload.get("path")
            manual = voice_item.payload.get("manual")

        sfx = None
        if sfx_item:
            sfx = {
                "effect_id": sfx_item.payload.get("effect_id"),
                "title": sfx_item.payload.get("title", ""),
                "duration_s": sfx_item.payload.get("duration_s"),
            }

        sticker_path = p.get("sticker")
        if not sticker_path and sticker_item:
            sticker_path = sticker_item.payload.get("path")
        sticker_info = p.get("sticker_info") or {}
        if sticker_item and not sticker_info:
            sticker_info = sticker_item.payload.get("sticker_info") or {}

        messages.append({
            "index": idx,
            "role": p.get("role", ""),
            "type": p.get("type", "text"),
            "text": p.get("text", ""),
            "emotion": p.get("emotion", "neutral"),
            "is_narration": bool(p.get("is_narration")),
            "start_s": round(it.start_s, 3),
            "end_s": round(it.end_s, 3),
            "duration_s": round(it.duration_s or (it.end_s - it.start_s), 3),
            "voice": voice_path,
            "sfx": sfx,
            "manual": manual,
            "sticker_path": sticker_path,
            "sticker_info": sticker_info,
            "overlay_sticker": p.get("overlay_sticker"),
            "overlay_sticker_info": p.get("overlay_sticker_info") or {},
            "scene_effect": p.get("scene_effect") or "",
        })

    missing = timeline.meta.get("missing_assets") or []
    estimates = {
        "total_duration_s": round(timeline.total_duration_s, 3),
        "message_count": len(msg_items),
        "voice_count": len([i for i in timeline.items_of("audio") if i.kind == "voice"]),
        "sfx_count": len([i for i in timeline.items_of("effect") if i.kind == "sfx"]),
        "sticker_count": len([i for i in timeline.items_of("effect") if i.kind == "sticker"]),
        "overlay_count": len([p for p in (i.payload for i in msg_items)
                              if p.get("overlay_sticker")]),
        "missing_count": len(missing),
        "asset_points_total": 0,
        "asset_points_used": timeline.meta.get("asset_points_used", 0),
    }

    return {
        "schema_version": 4,
        "title": timeline.title or "",
        "project_name": timeline.project_name or "GospelVideo",
        "resolution": str(timeline.resolution or 1080),
        "speaker": timeline.meta.get("speaker", ""),
        "bgm_query": timeline.bgm_query or "",
        "speedup": timeline.speedup or 1.0,
        "messages": messages,
        "missing_assets": missing,
        "song_mode": bool(timeline.song_mode),
        "song_audio": timeline.song_audio or "",
        "manual_narrations": timeline.meta.get("manual_narrations") or [],
        "estimates": estimates,
    }
