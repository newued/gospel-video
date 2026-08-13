# -*- coding: utf-8 -*-
"""gospel-video 核心引擎包（v5 架构）。

模块边界：
  parser           纯解析：文本 -> 消息列表（不含情绪/音色/角色名）
  emotion          情绪检测器：text -> emotion
  role_mapper      角色映射：speaker 标签 -> 角色名 + 音色 speaker_id
  alignment        对齐引擎：音频 -> 每句时间点（VAD/ASR/MANUAL 三种模式）
  audio_planner    音频规划：Dialogue -> 逐句音频素材（TTS/手动/整曲）
  visual_planner   视觉规划：Dialogue -> 贴纸/音效/动画/转场
  assets           素材提供者：AssetProvider 接口 + 内置实现
  timeline_planner 时间轴规划：各阶段结果 -> Timeline
  pipeline         编排：DAG 节点流水线
  renderer         渲染抽象：Timeline -> HTML/视频/剪映草稿

所有模块只操作 engine/models.py 中的 Dialogue/Message/Timeline 对象，
不互相传零散变量。
"""
