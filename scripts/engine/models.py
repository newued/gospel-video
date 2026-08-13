# -*- coding: utf-8 -*-
"""统一数据模型（v5 架构核心契约）。

所有模块（parser / role_mapper / emotion / alignment / audio_planner /
visual_planner / timeline_planner / pipeline / renderer）只操作本文件中的
对象，不再互相传递零散变量或平行数组。

对象层次：
  Dialogue
    ├─ messages: [Message]
    │    ├─ audio:  AudioClip     （TTS/手动/整曲的音频素材与时间）
    │    └─ visual: VisualSpec    （贴纸/音效/动画/转场等视觉规划）
    ├─ role_speakers: {role: speaker_id}
    ├─ assets: [Asset]            （素材提供者产出的素材）
    └─ meta: dict                 （任意扩展信息）
  Timeline                        （plan 升级版：真正的时间轴）
    └─ tracks: [Track{type, items}]
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# 基础数据结构
# ---------------------------------------------------------------------------

@dataclass
class AudioClip:
    """单条消息的音频素材与时间信息。"""
    path: str = ""                # 音频文件绝对路径（空表示无音频）
    duration_s: float = 0.0       # 音频自身时长
    start_s: float = 0.0          # 在时间轴上的起点
    end_s: float = 0.0            # 在时间轴上的终点
    source: str = "tts"           # tts | manual | song | none
    speedup: float = 1.0          # 实际施加的倍速
    manual: bool = False          # 需人工处理（旁白无音频）
    voice: str = ""               # 使用的 speaker_id
    original: str = ""            # 原始文件（变速/手动复制前的来源）


@dataclass
class VisualSpec:
    """单条消息的视觉规划（Visual Planner 输出）。"""
    side: str = "left"            # left | right | center（旁白）
    animation: str = ""           # 消息入场动画（Renderer 只执行，不决策）
    sticker: str = ""             # 贴纸路径（正式内容贴纸）
    overlay_sticker: str = ""     # 覆盖贴纸（下半屏居中，HTML 层渲染）
    sticker_info: Dict[str, Any] = field(default_factory=dict)
    overlay_sticker_info: Dict[str, Any] = field(default_factory=dict)
    transition: str = ""          # 转场（剪映）
    text_animation: str = ""      # 字幕文字动画（剪映）
    sfx: Dict[str, Any] = field(default_factory=dict)  # {effect_id,title,duration_s,source}
    avatar: str = ""              # 自定义头像路径
    color: str = ""               # 头像颜色
    exit_at: float = 0.0          # 退场时间点（Renderer 执行）
    scene_effect: str = ""        # 画面特效（剪映 identifier，如"烟花"/"震动"），由 visual_planner 归一化写入


@dataclass
class Message:
    """一条对话消息 —— 项目核心单元，全部模块围绕它工作。"""
    id: int = 0                   # 消息序号（与对话顺序一致）
    speaker: str = ""             # 原文标签 A/B/C
    role: str = ""                # 分配后的角色名（年轻人/大师/旁白）
    text: str = ""
    type: str = "text"            # text | sticker
    image: str = ""               # 图片消息（纯表情包消息）
    emotion: str = "neutral"      # angry | surprise | happy | sad | neutral
    narration: bool = False       # 是否旁白
    effects: list = None           # 原始 [[特效:...]] 标记 [{name,at,dur}]（解析阶段填入，visual_planner 消费后清空）
    audio: AudioClip = field(default_factory=AudioClip)
    visual: VisualSpec = field(default_factory=VisualSpec)

    # ---- 便捷属性（从 audio 派生） ----
    @property
    def start_s(self) -> float:
        return self.audio.start_s

    @property
    def end_s(self) -> float:
        return self.audio.end_s

    @property
    def duration_s(self) -> float:
        return max(0.0, self.audio.end_s - self.audio.start_s)


@dataclass
class Asset:
    """素材提供者产出的统一素材对象。"""
    type: str = ""                # sticker | sfx | transition | text_animation | avatar | bgm
    path: str = ""                # 素材路径（本地文件）
    name: str = ""                # 素材名称（如音效标题、转场名）
    emotion: str = ""             # 匹配用的情绪（可选）
    meta: Dict[str, Any] = field(default_factory=dict)
    provider: str = "builtin"     # 提供者标识（builtin/emoji/gif/custom）


@dataclass
class Dialogue:
    """剧本/对话的运行时对象 —— 各模块的输入输出统一容器。"""
    title: str = ""
    project_name: str = ""
    resolution: int = 1080
    speaker: str = "zh_male_huoli"
    bgm_query: str = ""
    speedup: float = 1.0
    messages: List[Message] = field(default_factory=list)
    role_speakers: Dict[str, str] = field(default_factory=dict)  # role -> speaker_id
    assets: List[Asset] = field(default_factory=list)   # 已选素材汇总
    meta: Dict[str, Any] = field(default_factory=dict)  # 任意扩展（song_mode等）

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        d = asdict(self)
        d["schema_version"] = 5
        return d

    def to_json(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "Dialogue":
        msgs = []
        for i, m in enumerate(data.get("messages", [])):
            audio = m.get("audio") or {}
            visual = m.get("visual") or {}
            msgs.append(Message(
                id=m.get("id", i),
                speaker=m.get("speaker", ""),
                role=m.get("role", ""),
                text=m.get("text", ""),
                type=m.get("type", "text"),
                image=m.get("image", ""),
                emotion=m.get("emotion", "neutral"),
                narration=bool(m.get("narration") or m.get("is_narration")),
                effects=m.get("effects"),
                audio=AudioClip(**{k: audio.get(k, v) for k, v in (
                    ("path", ""), ("duration_s", 0.0), ("start_s", 0.0),
                    ("end_s", 0.0), ("source", "tts"), ("speedup", 1.0),
                    ("manual", False), ("voice", ""), ("original", ""))}),
                visual=VisualSpec(**{k: visual.get(k, v) for k, v in (
                    ("side", "left"), ("animation", ""), ("sticker", ""),
                    ("overlay_sticker", ""), ("sticker_info", {}),
                    ("overlay_sticker_info", {}), ("transition", ""),
                    ("text_animation", ""), ("sfx", {}), ("avatar", ""),
                    ("color", ""), ("exit_at", 0.0))}),
            ))
        d = cls(
            title=data.get("title", ""),
            project_name=data.get("project_name", ""),
            resolution=data.get("resolution", 1080),
            speaker=data.get("speaker", "zh_male_huoli"),
            bgm_query=data.get("bgm_query", ""),
            speedup=data.get("speedup", 1.0),
            messages=msgs,
            role_speakers=data.get("role_speakers", {}),
            meta=data.get("meta", {}),
        )
        return d

    @classmethod
    def from_json(cls, path: str) -> "Dialogue":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    # ---- 兼容旧 script JSON（含 _tmp_script_*.json） ----
    @classmethod
    def from_legacy_script(cls, data: dict) -> "Dialogue":
        d = cls.from_dict(data)
        # 旧格式字段平移
        for i, m in enumerate(d.messages):
            if not m.speaker:
                m.speaker = m.role
            if not m.audio.voice:
                m.audio.voice = d.role_speakers.get(m.role, d.speaker)
        return d


# ---------------------------------------------------------------------------
# Timeline（plan.json 升级版）
# ---------------------------------------------------------------------------

@dataclass
class TimelineItem:
    """时间轴上的一个条目（Renderer/剪映只认这个）。"""
    type: str = ""                # message | audio | subtitle | effect
    kind: str = ""                # 细分：text | voice | song | sticker | sfx | transition | bgm ...
    start_s: float = 0.0
    end_s: float = 0.0
    duration_s: float = 0.0
    text: str = ""                # 字幕文本 / 说明
    payload: Dict[str, Any] = field(default_factory=dict)  # 素材路径等


@dataclass
class Track:
    """一条轨道。"""
    type: str = ""                # message | audio | subtitle | effect
    items: List[TimelineItem] = field(default_factory=list)


@dataclass
class Timeline:
    """真正的时间轴 —— Renderer 与剪映组装器的唯一输入。

    旧 plan.json 是"配置"，本对象是"时间轴"：
      tracks:
        message  消息气泡（谁、何时出现、动画）
        audio    音频（配音/整曲/BGM）
        subtitle 字幕（文本+文字动画）
        effect   效果（贴纸/音效/转场）
    """
    schema_version: int = 5
    project_name: str = ""
    title: str = ""
    resolution: int = 1080
    total_duration_s: float = 0.0
    speedup: float = 1.0
    bgm_query: str = ""
    song_mode: bool = False
    song_audio: str = ""          # 整曲音频路径（song_mode 时）
    tracks: List[Track] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)

    def track(self, type_: str) -> Track:
        for t in self.tracks:
            if t.type == type_:
                return t
        t = Track(type=type_)
        self.tracks.append(t)
        return t

    def items_of(self, type_: str) -> List[TimelineItem]:
        for t in self.tracks:
            if t.type == type_:
                return t.items
        return []

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict) -> "Timeline":
        t = cls(
            schema_version=data.get("schema_version", 5),
            project_name=data.get("project_name", ""),
            title=data.get("title", ""),
            resolution=data.get("resolution", 1080),
            total_duration_s=data.get("total_duration_s", 0.0),
            speedup=data.get("speedup", 1.0),
            bgm_query=data.get("bgm_query", ""),
            song_mode=data.get("song_mode", False),
            song_audio=data.get("song_audio", ""),
            meta=data.get("meta", {}),
        )
        for td in data.get("tracks", []):
            tr = Track(type=td.get("type", ""))
            for it in td.get("items", []):
                tr.items.append(TimelineItem(**{k: it.get(k, v) for k, v in (
                    ("type", ""), ("kind", ""), ("start_s", 0.0), ("end_s", 0.0),
                    ("duration_s", 0.0), ("text", ""), ("payload", {}))}))
            t.tracks.append(tr)
        return t

    @classmethod
    def from_json(cls, path: str) -> "Timeline":
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


# ---------------------------------------------------------------------------
# Hook 上下文（贯穿全流水线的运行时对象）
# ---------------------------------------------------------------------------

@dataclass
class PipelineContext:
    """流水线上下文：Hook 与 DAG 节点共享。"""
    stage: str = ""               # 当前阶段名
    source: Dict[str, Any] = field(default_factory=dict)  # 原始输入
    dialogue: Optional[Dialogue] = None
    timeline: Optional[Timeline] = None
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    results: Dict[str, Any] = field(default_factory=dict)  # 各节点产物（音频、渲染等）
    config: Dict[str, Any] = field(default_factory=dict)   # CLI 参数等
