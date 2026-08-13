# -*- coding: utf-8 -*-
"""对账闸门（QA Gate）—— gospel-video v6 工程化升级核心。

设计思想（来自《Codex Skill 全流程拆解》的"闸门思维"）：
  不追求一步到位，而是在最容易出错的环节先卡一道检查，把错误拦在
  花算力生成视频 / 组装剪映草稿之前（此时错误最便宜）。

本模块在 Timeline 构建完成、渲染/组装之前运行，对账范围：
  - 台词 ↔ 配音时间：每条消息是否有有效时间窗（start<end，时长合理）
  - 气泡连续性：相邻气泡是否重叠 / 是否出现负时长 / 是否长时间空白
  - 表情包 ↔ 素材：sticker / overlay_sticker 指向的文件是否真实存在
  - 音效 ↔ 素材：sfx 决策是否落到真实素材（或已记录缺失）
  - 旁白/无音频：旁白未自动配音、普通消息 source=none 需人工处理
  - 总时长合理性：最后一条消息时间窗与 total_duration 的尾留

对外契约：
  run_qa(dialogue, timeline=None, strict=False) -> dict
    返回 {ok, errors, warnings, issues, report, abort}
    - ok: 无 error 级别问题
    - abort: strict=True 且存在 error 时建议中断（由调用方决定）
  format_qa_report(result) -> str   人类可读报告（控制台 / AI 汇报）

另含「复刻模式」拆解提示词常量 VIDEO_DECOMPOSE_PROMPT 与 build_replicate_prompt()，
用于在 AI 助手中把一条爆款聊天视频拆成本 skill 的剧本 JSON（见 SKILL.md 复刻模式）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from .models import Dialogue, Timeline


# ---------------------------------------------------------------------------
# 检查项阈值（可在调用时覆盖）
# ---------------------------------------------------------------------------
DEFAULT_MIN_MSG_DUR = 0.3      # 单条消息最短时长（秒），短于此视为无效
DEFAULT_MAX_OVERLAP = 0.3      # 相邻气泡允许的最大重叠（秒），超过报警
DEFAULT_TAIL_GAP = 0.2         # 最后一条与总时长的最小尾留（秒）


@dataclass
class QAIssue:
    """单条对账问题。"""
    severity: str          # "error" | "warning"
    category: str          # audio | visual | continuity | asset | structure
    index: int             # 消息序号（-1 表示全局/非特定消息）
    message: str

    def __str__(self) -> str:
        tag = "❌" if self.severity == "error" else "⚠️"
        loc = f"#{self.index:02d}" if self.index >= 0 else "全局"
        return f"  {tag} [{self.category}] {loc}: {self.message}"


# ---------------------------------------------------------------------------
# 资源定位（与 visual_planner 口径一致：相对路径按项目根解析）
# ---------------------------------------------------------------------------

def _resolve_exists(path: str) -> bool:
    if not path:
        return False
    if os.path.isabs(path) and os.path.exists(path):
        return True
    if os.path.isabs(path):
        return False
    # 相对路径：尝试在多个候选根下定位（scripts/ 与项目根）
    here = os.path.dirname(os.path.abspath(__file__))
    engine_root = os.path.dirname(here)
    project_root = os.path.dirname(engine_root)
    for base in (engine_root, project_root, os.getcwd()):
        if os.path.exists(os.path.join(base, path)):
            return True
    return os.path.exists(path)


# ---------------------------------------------------------------------------
# 对账主入口
# ---------------------------------------------------------------------------

def run_qa(dialogue: Dialogue, timeline: Optional[Timeline] = None,
           strict: bool = False,
           min_msg_dur: float = DEFAULT_MIN_MSG_DUR,
           max_overlap: float = DEFAULT_MAX_OVERLAP,
           tail_gap: float = DEFAULT_TAIL_GAP) -> dict:
    """对账闸门：逐项核对 Dialogue（及可选 Timeline），返回结构化结果。

    Args:
        dialogue: 已规划音频/视觉的 Dialogue（visual_planner 之后）
        timeline: 可选，已构建的 Timeline（若提供则额外核对轨道一致性）
        strict:   True 时存在 error 级别问题则 abort=True（建议中断生成）
        min_msg_dur / max_overlap / tail_gap: 阈值覆盖

    Returns:
        {
          ok: bool,            # 无 error
          abort: bool,         # strict 且存在 error
          errors: List[str],
          warnings: List[str],
          issues: List[QAIssue],
          report: str,         # 人类可读报告
          stats: dict,         # 计数统计
        }
    """
    issues: List[QAIssue] = []

    msgs = dialogue.messages
    if not msgs:
        issues.append(QAIssue("error", "structure", -1, "对话没有任何消息，无法生成视频"))
        return _finalize(issues, strict)

    # ---- 1. 逐条消息：台词 / 配音时间 / 素材 ----
    prev_end = 0.0
    for i, m in enumerate(msgs):
        # 台词非空
        if not (m.text or "").strip() and m.type == "text":
            issues.append(QAIssue("warning", "structure", i, "消息文本为空（空气泡）"))

        a = m.audio
        # 时间窗有效性
        if a.end_s <= a.start_s:
            issues.append(QAIssue(
                "error", "audio", i,
                f"时间窗无效（start={a.start_s:.2f} >= end={a.end_s:.2f}），配音未对齐"))
        elif (a.end_s - a.start_s) < min_msg_dur:
            issues.append(QAIssue(
                "warning", "audio", i,
                f"时长过短（{a.end_s - a.start_s:.2f}s < {min_msg_dur}s），气泡一闪而过"))

        # 无音频 / 需人工
        if a.source in ("none",) and not m.narration:
            issues.append(QAIssue(
                "warning", "audio", i,
                "无配音（TTS 失败或未提供音频），该条将无声音，需人工补录"))
        if m.narration and a.manual:
            issues.append(QAIssue(
                "warning", "audio", i,
                "旁白未自动配音（不在整曲中/无 TTS），需在剪映手动补旁白配音"))

        # 相邻气泡重叠
        if i > 0 and a.start_s < prev_end - 1e-6:
            overlap = prev_end - a.start_s
            if overlap > max_overlap:
                issues.append(QAIssue(
                    "warning", "continuity", i,
                    f"与上一条气泡重叠 {overlap:.2f}s（> {max_overlap}s），画面可能穿帮"))

        # 表情包 / 覆盖贴纸素材存在性
        v = m.visual
        for label, p in (("贴纸", v.sticker), ("覆盖贴纸", v.overlay_sticker)):
            if p and not _resolve_exists(p):
                issues.append(QAIssue(
                    "warning", "asset", i,
                    f"{label}素材不存在：{p}（落草稿时该贴图会被跳过，请补素材或改路径）"))

        # 音效素材
        sfx = v.sfx or {}
        sfx_title = sfx.get("title") or ""
        sfx_eff = sfx.get("effect_id") or ""
        if sfx_title and not sfx_eff:
            # 自动匹配未拿到剪映 effect_id，仅提示（运行时按标题在剪映素材库搜）
            issues.append(QAIssue(
                "warning", "asset", i,
                f"音效「{sfx_title}」未绑定剪映 effect_id，导出后需人工在剪映音频库确认"))

        # 画面特效（scene_effect 为剪映 identifier；未知模板会在导出时由 _resolve_enum 兜底，
        # 这里仅在校验层面提示，不阻断）
        se = (v.scene_effect or "").strip()
        if se:
            from .effects_catalog import category_of
            if category_of(se) == "未知":
                issues.append(QAIssue(
                    "warning", "asset", i,
                    f"画面特效「{se}」未在内置分类中，导出时将按原名在剪映素材库匹配；"
                    f"若剪映无此模板则需手动替换"))

        prev_end = max(prev_end, a.end_s)

    # ---- 2. 视觉规划缺失清单（visual_planner 已记录） ----
    for miss in dialogue.meta.get("missing_assets", []) or []:
        idx = miss.get("index", -1)
        detail = miss.get("detail", "")
        issues.append(QAIssue("warning", "asset", idx, f"素材缺失：{detail}"))

    # ---- 3. Timeline 轨道一致性（若提供） ----
    if timeline is not None:
        # 消息轨条数应与 dialogue 消息数一致
        msg_items = timeline.items_of("message")
        if len(msg_items) != len(msgs):
            issues.append(QAIssue(
                "error", "structure", -1,
                f"时间轴消息轨 {len(msg_items)} 条，与剧本 {len(msgs)} 条不一致"))
        # 轨道总时长兜底
        if timeline.total_duration_s <= 0:
            issues.append(QAIssue(
                "warning", "structure", -1, "时间轴总时长未设置或为 0"))

    # ---- 4. 总时长尾留 ----
    total = dialogue.meta.get("total_duration_s", 0.0) or (
        timeline.total_duration_s if timeline else 0.0)
    if total > 0 and prev_end > 0:
        gap = total - prev_end
        if gap < tail_gap:
            issues.append(QAIssue(
                "warning", "continuity", -1,
                f"结尾尾留仅 {gap:.2f}s（< {tail_gap}s），最后一条可能被截断"))

    return _finalize(issues, strict)


def _finalize(issues: List[QAIssue], strict: bool) -> dict:
    errors = [str(x) for x in issues if x.severity == "error"]
    warnings = [str(x) for x in issues if x.severity == "warning"]
    ok = len(errors) == 0
    return {
        "ok": ok,
        "abort": (strict and not ok),
        "errors": errors,
        "warnings": warnings,
        "issues": issues,
        "report": format_qa_report({
            "ok": ok, "abort": (strict and not ok),
            "errors": errors, "warnings": warnings, "issues": issues,
        }),
        "stats": {
            "error": len(errors),
            "warning": len(warnings),
            "total": len(issues),
        },
    }


def format_qa_report(result: dict) -> str:
    """把对账结果渲染成人类可读报告。"""
    issues = result.get("issues", [])
    errors = result.get("errors", [])
    warnings = result.get("warnings", [])
    lines = []
    lines.append("=" * 56)
    lines.append("🛡️ 对账闸门（QA Gate）报告")
    lines.append("=" * 56)
    if not issues:
        lines.append("  ✅ 全部通过，无问题。")
    else:
        if errors:
            lines.append(f"  ❌ 错误 {len(errors)} 项（建议修复后再生成）：")
        if warnings:
            lines.append(f"  ⚠️ 警告 {len(warnings)} 项（不阻断，建议确认）：")
        for x in issues:
            lines.append(str(x))
    lines.append("-" * 56)
    verdict = "通过，可继续生成" if result.get("ok") else "存在问题，请查看上方项"
    if result.get("abort"):
        verdict = "严格模式下存在错误，已建议中断生成"
    lines.append(f"  结论：{verdict}")
    lines.append("=" * 56)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 复刻模式（Replicate Mode）—— 拆解提示词
# ---------------------------------------------------------------------------
# 来自《Codex Skill 全流程拆解》的镜头级拆解思路，浓缩为适配本 skill 的
# 剧本 JSON 输出契约。在 AI 助手（Codex / WorkBuddy）中，把一条爆款聊天
# 视频丢给大模型，要求其按此契约输出本 skill 的剧本 JSON（见 SKILL.md）。
VIDEO_DECOMPOSE_PROMPT = """你是「微信聊天爆款视频拆解助手」。

请观看我提供的聊天记录风格短视频，把它拆成可被 gospel-video skill 直接消费的剧本 JSON。

# 拆解目标
1. 识别聊天双方角色（如 A=老板 / B=员工，或按视频中的昵称），给出稳定角色名
2. 按时间顺序提取每条聊天气泡的文本（保留原意，可适度口语化润色但不篡改笑点）
3. 标注每条消息的：
   - role: 发送方角色名
   - emotion: 该条情绪（angry / surprise / happy / sad / neutral）
   - 是否旁白（屏幕居中半透明黑底白字，通常视频里的"X分钟后""与此同时"这类）
4. 标注整体 BGM 风格关键词（如 搞笑 / 悬疑 / 国风 / 魔性）
5. 标注强喜剧/强情绪句（用于自动配音效与表情包）

# 输出 JSON 格式（schema_version=5，可直接喂给 gospel_automator.py）
{
  "title": "视频标题（提炼）",
  "bgm_query": "搞笑 悬疑 国风",
  "role_speakers": {"角色A名": "", "角色B名": ""},
  "messages": [
    {"role": "角色A名", "text": "...", "emotion": "happy"},
    {"role": "旁白", "text": "一分钟后", "is_narration": true},
    {"role": "角色B名", "text": "...", "emotion": "angry"}
  ]
}

# 规则
- 只输出 JSON，不要 Markdown 代码块包裹，不要写总结
- 旁白消息必须带 is_narration: true
- 文本基于视频真实内容，听不清处用 [听不清] 占位
- emotion 缺省 neutral；强情绪才标 angry/surprise/happy/sad
"""

# 复刻模式推荐的后续流程（AI 助手内联指引）
REPLICATE_WORKFLOW = """复刻模式后续步骤：
1. 把上面拆出的 JSON 保存为 <name>.json
2. 用模型直通模式渲染（大模型已决策 emotion，脚本机械执行）：
   python scripts/gospel_automator.py <name>.json --plan-only
   → 审核 plan / 聊天视频的素材匹配是否满意
3. 满意后去掉 --plan-only 生成完整剪映草稿：
   python scripts/gospel_automator.py <name>.json
4. （可选）如需替换音乐为Suno/妙响整曲，先 -m suno 生成提示词再 -m manual
"""


def build_replicate_prompt(reference_note: str = "") -> str:
    """构造复刻模式完整提示（拆解提示词 + 工作流）。

    Args:
        reference_note: 用户对参考视频的补充说明（可选）
    """
    note = f"\n# 参考视频补充说明\n{reference_note}\n" if reference_note else ""
    return VIDEO_DECOMPOSE_PROMPT + note + "\n" + REPLICATE_WORKFLOW
