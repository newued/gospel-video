# -*- coding: utf-8 -*-
"""DAG 编排：把 v5 架构的各个引擎节点串成一条可重跑流水线。

节点顺序（见 docs/ARCHITECTURE_v5.md）：
  ParserNode -> AudioPlannerNode -> AlignNode -> VisualPlannerNode
  -> TimelineNode -> RenderNode -> AssembleNode

设计要点：
  - 每个节点只操作 ctx（PipelineContext），run 返回 ctx
  - 每个节点 run 前后触发 HOOKS.run("before_<point>"/"after_<point>")
  - 配置参数统一放 ctx.source（本环境 PipelineContext 无 config 字段）：
      speedup / audio_mode / custom_dir / tts_dir / align_mode /
      chat_video / draft_root / asset_dir / width / height ...
  - RenderNode / AssembleNode 失败记 ctx.errors 不中断整条流水线
"""
import os

from engine.models import Dialogue, PipelineContext
from engine.hooks import HOOKS

# 节点 name -> Hook 点位名 的映射
_POINT_MAP = {
    "parser": "parse",
    "audio_planner": "audio",
    "align": "alignment",
    "visual_planner": "visual",
    "timeline": "timeline",
    "qa": "qa",
    "render": "render",
    "library": "library",
    "assemble": "assemble",
}


class Node:
    """流水线节点基类。"""

    name: str = ""

    def run(self, ctx: PipelineContext) -> PipelineContext:
        raise NotImplementedError(f"{type(self).__name__}.run 未实现")


class ParserNode(Node):
    """解析文本 -> Dialogue（parser + role_mapper + emotion 一体完成）。"""

    name = "parser"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        source = ctx.source or {}
        raw = source.get("text") or ""
        file_path = source.get("file") or ""

        from engine.parser import parse_text, parse_file, extract_voice_hints, infer_bgm_query

        if raw:
            raws = parse_text(raw)
            hints = extract_voice_hints(raw)
            bgm = infer_bgm_query(raw)
        elif file_path:
            raws = parse_file(file_path)
            hints = {}
            bgm = infer_bgm_query("")
        else:
            ctx.errors.append("ParserNode: ctx.source 需要 text 或 file 字段")
            return ctx

        from engine.role_mapper import map_roles

        default_speaker = source.get("speaker") or "zh_male_huoli"
        messages, role_speakers = map_roles(raws, hints, default_speaker=default_speaker)

        ctx.dialogue = Dialogue(
            title=source.get("title") or "",
            project_name=source.get("project_name") or source.get("title") or "GospelVideo",
            resolution=source.get("resolution") or 1080,
            speaker=default_speaker,
            bgm_query=source.get("bgm_query") or bgm,
            speedup=float(source.get("speedup") or 1.3),
            messages=messages,
            role_speakers=role_speakers,
            meta=dict(source.get("meta") or {}),
        )
        ctx.results["parser"] = {
            "message_count": len(messages),
            "role_speakers": role_speakers,
        }
        return ctx


class AudioPlannerNode(Node):
    """调用 engine.audio_planner.plan_audio 规划逐句音频。

    缺失输入处理：TTS 网络不可用 / 素材缺失时，降级为文本时长估算对齐，
    保证后续节点仍能拿到时间点。
    """

    name = "audio_planner"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        d = ctx.dialogue
        if d is None:
            ctx.errors.append("AudioPlannerNode: 缺少 ctx.dialogue（需先跑 ParserNode）")
            return ctx

        mode = ctx.source.get("audio_mode", "auto")
        speedup = float(ctx.source.get("speedup") or d.speedup or 1.3)
        tts_dir = ctx.source.get("tts_dir") or ""
        custom_dir = (ctx.source.get("custom_dir") or "") if mode == "manual" else ""

        try:
            from engine.audio_planner import plan_audio

            plan_audio(d, mode=mode, custom_dir=custom_dir, tts_dir=tts_dir, speedup=speedup)
            ctx.results["audio_mode"] = mode
        except Exception as e:
            # 降级：无音频 -> 文本估算对齐（AlignmentEngine 内部回填 start/end）
            ctx.warnings.append(f"plan_audio 失败，改用文本估算对齐: {e}")
            try:
                from engine.alignment import AlignmentEngine

                AlignmentEngine(mode="AUTO").align(d)
                ctx.results["audio_mode"] = "estimate"
            except Exception as e2:
                ctx.errors.append(f"音频规划失败: {e2}")
        return ctx


class AlignNode(Node):
    """调用 engine.alignment.AlignmentEngine 对齐时间点（mode 来自 config）。

    若 plan_audio 已回填逐句时间点（tts_timings/manual_timings），则跳过，
    避免覆盖已有对齐结果。
    """

    name = "align"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        d = ctx.dialogue
        if d is None:
            ctx.errors.append("AlignNode: 缺少 ctx.dialogue")
            return ctx

        method = d.meta.get("alignment_method") or ""
        if method in ("tts_timings", "manual_timings"):
            ctx.warnings.append(f"timings 已由 audio_planner 填充（{method}），AlignNode 跳过")
            return ctx

        try:
            mode = ctx.source.get("align_mode", "AUTO")
            from engine.alignment import AlignmentEngine

            eng = AlignmentEngine(mode=mode)
            audio_path = d.meta.get("song_audio") or None
            timings_path = ctx.source.get("timings_path")
            eng.align(d, audio_path=audio_path, timings_path=timings_path)
        except Exception as e:
            ctx.errors.append(f"对齐失败: {e}")
        return ctx


class VisualPlannerNode(Node):
    """调用 engine.visual_planner.plan_visual 规划贴纸/音效/动画/转场。"""

    name = "visual_planner"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        d = ctx.dialogue
        if d is None:
            ctx.errors.append("VisualPlannerNode: 缺少 ctx.dialogue")
            return ctx
        try:
            from engine.visual_planner import plan_visual

            plan_visual(d)
        except Exception as e:
            ctx.errors.append(f"视觉规划失败: {e}")
        return ctx


class TimelineNode(Node):
    """调用 timeline_planner.build_timeline 生成时间轴。"""

    name = "timeline"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        d = ctx.dialogue
        if d is None:
            ctx.errors.append("TimelineNode: 缺少 ctx.dialogue")
            return ctx
        try:
            from engine.timeline_planner import build_timeline

            ctx.timeline = build_timeline(d)
        except Exception as e:
            ctx.errors.append(f"时间轴构建失败: {e}")
        return ctx


class QAGateNode(Node):
    """对账闸门（v6）：Timeline 构建后、渲染前，逐项核对。

    把错误拦在花算力生成视频之前。strict 模式存在 error 时把 abort 写入 ctx，
    由调用方决定中断。默认不强制中断（保留离线容错）。
    """

    name = "qa"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        d = ctx.dialogue
        if d is None:
            ctx.errors.append("QAGateNode: 缺少 ctx.dialogue")
            return ctx
        try:
            from engine.qa import run_qa, format_qa_report

            strict = bool(ctx.source.get("strict_qa", False))
            result = run_qa(d, timeline=ctx.timeline, strict=strict)
            ctx.results["qa"] = result
            print(format_qa_report(result))
            if result.get("abort"):
                ctx.errors.append("对账闸门(strict)未通过，已建议中断生成")
        except Exception as e:
            ctx.warnings.append(f"对账闸门运行失败（不阻断）: {e}")
        return ctx


class LibraryNode(Node):
    """资产沉淀（v6）：归档本次剧本到 assets/library/，便于跨项目复用。"""

    name = "library"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        d = ctx.dialogue
        if d is None:
            return ctx
        try:
            from engine.library import archive_project

            qa = ctx.results.get("qa")
            rec_path = archive_project(d, qa_result=qa)
            ctx.results["library"] = rec_path
        except Exception as e:
            ctx.warnings.append(f"资产归档失败（不阻断）: {e}")
        return ctx


class RenderNode(Node):
    """调用 engine.renderer.render_chat（timeline 驱动）渲染聊天视频。"""

    name = "render"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.timeline is None or ctx.dialogue is None:
            ctx.errors.append("RenderNode: 缺少 timeline/dialogue")
            return ctx
        try:
            from engine.renderer import render_chat, default_chat_video_path

            out_video = ctx.source.get("chat_video") or default_chat_video_path(
                ctx.timeline.project_name)
            width = int(ctx.source.get("width") or 1080)
            height = int(ctx.source.get("height") or 1920)
            asset_dir = ctx.source.get("asset_dir") or ""
            render_chat(ctx.timeline, ctx.dialogue, out_video,
                        width=width, height=height, asset_dir=asset_dir)
            ctx.results["chat_video"] = out_video
        except Exception as e:
            ctx.errors.append(f"渲染失败: {e}")
        return ctx


class AssembleNode(Node):
    """调用 gospel_automator._assemble_draft 做剪映组装（import 兼容旧入口）。

    输入 timeline 转回旧 plan dict（timeline_to_legacy_plan），旧组装代码零改动。
    """

    name = "assemble"

    def run(self, ctx: PipelineContext) -> PipelineContext:
        if ctx.timeline is None:
            ctx.errors.append("AssembleNode: 缺少 timeline")
            return ctx
        try:
            from engine.timeline_planner import timeline_to_legacy_plan

            plan = timeline_to_legacy_plan(ctx.timeline)
            from gospel_automator import _assemble_draft

            result = _assemble_draft(
                plan,
                draft_root=ctx.source.get("draft_root") or None,
                export_video=ctx.source.get("export_video") or None,
                asset_dir=ctx.source.get("asset_dir") or None,
            )
            ctx.results["assemble_result"] = result
            ctx.results["draft_path"] = result.get("draft_path")
            ctx.results["chat_video"] = result.get("chat_video") or ctx.results.get("chat_video")
            if not result.get("ok"):
                ctx.errors.append(result.get("errors") or "剪映组装失败")
        except Exception as e:
            ctx.errors.append(f"剪映组装失败: {e}")
        return ctx


class Pipeline:
    """顺序执行的节点流水线（支持 from_node 局部重跑）。"""

    def __init__(self) -> None:
        self.nodes = []

    def add(self, node: Node) -> "Pipeline":
        if not isinstance(node, Node):
            raise TypeError(f"Pipeline.add: 期望 Node，得到 {type(node).__name__}")
        self.nodes.append(node)
        return self

    def execute(self, ctx: PipelineContext, from_node: str = "") -> PipelineContext:
        """顺序执行节点。

        from_node 非空时从该节点（含）开始执行，支持 DAG 局部重跑。
        每个节点 run 前后触发 before_<point> / after_<point> Hook。
        """
        names = [n.name for n in self.nodes]
        if from_node:
            if from_node not in names:
                raise ValueError(f"execute: 未知节点 {from_node}（可用: {names}）")
            start = names.index(from_node)
        else:
            start = 0

        for node in self.nodes[start:]:
            point = _POINT_MAP.get(node.name, node.name)
            ctx.stage = node.name
            ctx = HOOKS.run(f"before_{point}", ctx)
            ctx = node.run(ctx)
            ctx = HOOKS.run(f"after_{point}", ctx)
        ctx.stage = ""
        return ctx

    def rerun_from(self, node_name: str, ctx: PipelineContext) -> PipelineContext:
        """从指定节点重跑（语义别名，等同 execute(ctx, from_node=...)。"""
        return self.execute(ctx, from_node=node_name)


def build_default_pipeline(render_enabled: bool = True,
                           assemble_enabled: bool = True,
                           qa_enabled: bool = True,
                           library_enabled: bool = True) -> Pipeline:
    """构建默认流水线。

    render_enabled=False / assemble_enabled=False 时跳过渲染与剪映组装节点，
    用于纯规划链路（解析->音频->对齐->视觉->时间轴）。
    qa_enabled：是否在时间轴后插入对账闸门（默认开启，不阻断）。
    library_enabled：是否归档到 assets/library/（默认开启）。
    """
    p = Pipeline()
    p.add(ParserNode())
    p.add(AudioPlannerNode())
    p.add(AlignNode())
    p.add(VisualPlannerNode())
    p.add(TimelineNode())
    if qa_enabled:
        p.add(QAGateNode())
    if render_enabled:
        p.add(RenderNode())
    if library_enabled:
        p.add(LibraryNode())
    if assemble_enabled:
        p.add(AssembleNode())
    return p
