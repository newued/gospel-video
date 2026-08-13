# -*- coding: utf-8 -*-
"""
gospel_automator.py — 福音吐槽视频流水线（v5 薄封装版）
=======================================================

本文件保留全部旧 API 与 CLI 兼容，内部改走 engine 流水线：

  build_plan   -> Dialogue.from_legacy_script
               -> Pipeline（AudioPlannerNode/AlignNode/VisualPlannerNode/TimelineNode）
               -> timeline_to_legacy_plan 落盘 plan.json（schema_version=4）
  _assemble_draft -> 原剪映组装实现（原样保留，继续消费旧格式 plan dict）

对外依赖方：
  - gospel_dialog.py  依赖 build_gospel_video + _tts_segment/_speedup_audio（patch 机制）
  - engine/pipeline.py 的 AssembleNode 在运行时 import _assemble_draft

用法：
  python gospel_automator.py 剧本.json                # 完整流水线（自动生成 plan）
  python gospel_automator.py 剧本.json --plan my.json # 使用已有 plan（人工可改）
  python gospel_automator.py 剧本.json --plan-only    # 只到阶段2，留待人工补素材
  python gospel_automator.py 剧本.json --export out.mp4  # 额外导出（需剪映开着）
"""
import argparse
import asyncio
import json
import os
import random
import shutil
import subprocess
import sys
import time

# Windows 控制台避免 GBK 编码崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _locate_jy_skill() -> str:
    """定位 jianying-editor skill 根目录：环境变量 JY_SKILL_ROOT 优先，兜底常见安装位置。

    jianying-editor 是可选依赖（完整流水线的 TTS/原生贴纸/云素材能力需要它）；
    缺失时本模块部分能力降级，ab_generator.py 的 AB 模式不受影响。
    """
    env = os.environ.get("JY_SKILL_ROOT")
    if env and os.path.isdir(os.path.join(env, "scripts")):
        return env
    for cand in (
        os.path.expanduser(r"~/.config/opencode/skills/jianying-editor"),
        os.path.expanduser(r"~/.agents/skills/jianying-editor"),
    ):
        if os.path.isdir(os.path.join(cand, "scripts")):
            return cand
    if env:
        return env
    print("[warn] 未找到 jianying-editor skill（可设置环境变量 JY_SKILL_ROOT 指向其根目录），"
          "完整流水线的 TTS/原生贴纸/云素材能力将降级；AB 对话模式不受影响。")
    return ""


SKILL_ROOT = _locate_jy_skill()
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(PROJECT_ROOT, "assets")
CHAT_VIDEO_DIR = os.path.join(ASSETS_DIR, "chat_video")
TTS_DIR = os.path.join(ASSETS_DIR, "tts")
DEFAULT_PLAN = os.path.join(ASSETS_DIR, "plan.json")

sys.path.insert(0, os.path.join(SKILL_ROOT, "scripts"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
# pyJianYingDraft：优先仓库内置 vendor（开源随仓库分发），jianying-editor 的 vendor 兜底
for _v in (os.path.join(PROJECT_ROOT, "vendor"),
           os.path.join(SKILL_ROOT, "scripts", "vendor") if SKILL_ROOT else ""):
    if _v and os.path.isdir(os.path.join(_v, "pyJianYingDraft")):
        sys.path.insert(0, _v)
        sys.path.insert(0, os.path.join(_v, "pyJianYingDraft"))
        break

# 以下导入依赖上面 sys.path（模块顶层注入，确保 _locate_jy_skill 先行）。
# universal_tts / utils.formatters 来自 jianying-editor skill（可选依赖），缺失时给出明确提示。
try:
    from chat_scene_renderer import render_chat_scene  # noqa: E402
    from media_utils import compute_sticker_scale  # noqa: E402
    from random_assets import (  # noqa: E402
        pick_sticker,
        random_text_animation,
        random_transition,
        random_tts_speaker,
        sfx_for_emotion,
    )
    from universal_tts import generate_voice  # noqa: E402
    from utils.formatters import get_duration_ffprobe_cached  # noqa: E402
except ImportError as _e:
    raise ImportError(
        "gospel_automator 完整流水线依赖 jianying-editor skill（提供 universal_tts / "
        "utils / 云素材库）。请安装 jianying-editor 并设置环境变量 JY_SKILL_ROOT 指向其根目录；"
        "若只需 AB 双人对话视频，请改用 scripts/ab_generator.py（无需 jianying-editor）。"
        "原始错误: %s" % _e)

DEFAULT_SPEAKER = "zh_male_huoli"
DEFAULT_SPEEDUP = 1.3      # 整体节奏倍速（>1 更快）：配音变速 + 时序/动画同步压缩
MSG_GAP_S = 0.4            # 消息间节奏留白（秒）
FIRST_MSG_OFFSET_S = 0.5   # 首条消息延迟（秒）
TAIL_PADDING_S = 1.5       # 结尾缓冲（秒）
BGM_VOLUME = 0.35          # BGM 音量降低，避免盖过人声
MIN_TTS_BYTES = 500        # 小于此字节数视为残缺/无效音频
ASSET_POINT = 1            # 每个自动匹配的云素材计 1 积分（预算参考）


# 画布默认竖屏 1080×1920（9:16）。gospel-video 所有草稿 / 聊天视频均按此输出，
# 单一来源，避免被某条路径改回横屏。剪映坐标系：transform_x/y 单位 = 半个画布宽/高。
CANVAS_WIDTH = 1080
CANVAS_HEIGHT = 1920

# 剪映原生贴纸自动匹配（来自 jianying-editor 的 StickerOpsMixin）。
# True=在组装阶段按消息文本/情绪自动选贴并叠加到 sticker 轨道；
# 设为 False 可退回仅依赖聊天卡片内烘焙的本地表情图。
NATIVE_STICKERS_ENABLED = True


def _fmt_s(t: float) -> str:
    """秒转 'Ns' 字符串，保留一位小数（skill 的 safe_tim 支持）"""
    return f"{round(t, 1)}s"


def _load_script(script_path: str) -> dict:
    """读取剧本 JSON 文件。"""
    with open(script_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _valid_audio(path: str) -> bool:
    """校验音频文件有效（存在且体积足够，避免残缺文件混入草稿）"""
    if not path or not os.path.exists(path):
        return False
    try:
        return os.path.getsize(path) >= MIN_TTS_BYTES
    except OSError:
        return False


def _find_ffmpeg() -> str | None:
    """定位 ffmpeg：优先使用 imageio-ffmpeg（版本可控、格式兼容好），其次系统 PATH。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    return None


def _speedup_audio(audio_path: str, factor: float, index: int) -> str:
    """用 ffmpeg atempo 对配音变速（音调不变），输出 voice_NNN_x{factor}.mp3。

    变速失败/ffmpeg 缺失时返回原路径（容错，节奏略慢但不中断流水线）。

    注意：本函数为 3 参版本，保留旧签名，供 gospel_dialog 的逐句手动音频
    模式 patch（_no_speedup 替换）；薄封装流水线内由 engine.audio_planner
    完成变速（其 4 参版本实现一致）。
    """
    if not audio_path or factor <= 1.0001 or not _valid_audio(audio_path):
        return audio_path
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        print("警告: ffmpeg 不可用，跳过配音变速（--speed 不生效，按原速继续）")
        return audio_path
    out = os.path.join(TTS_DIR, f"voice_{index:03d}_x{factor:g}.mp3")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-i", audio_path, "-filter:a", f"atempo={factor}",
           "-c:a", "libmp3lame", "-q:a", "2", out]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=120)
        if proc.returncode == 0 and _valid_audio(out):
            print(f"配音[{index}] 变速 {factor} 倍: {os.path.basename(out)}")
            return out
        print(f"配音[{index}] 变速失败，使用原速")
    except Exception as e:
        print(f"配音[{index}] 变速异常: {e}")
    return audio_path


_ORIG_SPEEDUP_AUDIO = _speedup_audio


def _tts_segment(text: str, speaker: str, index: int) -> str:
    """为单条消息生成配音，返回音频路径或 None；残缺文件自动删除并重试。

    注意：本函数为 3 参版本，保留旧签名，供 gospel_dialog 的逐句手动音频
    模式 patch（_patched_tts 替换后回退到这里）。薄封装流水线内的逐句 TTS
    由 engine.audio_planner 完成（其 4 参版本实现一致）。
    """
    os.makedirs(TTS_DIR, exist_ok=True)
    out = os.path.join(TTS_DIR, f"voice_{index:03d}.mp3")
    try:
        result = asyncio.run(
            generate_voice(text, out, speaker=speaker, allow_fallback=True)
        )
        if _valid_audio(result):
            return result
        if result:
            # SAMI 静默失败可能产出残缺文件（如 172B 空 OGG），清理后强制 edge-tts 重试
            print(f"TTS[{index}] 输出文件无效（{os.path.getsize(result)} 字节），改用 edge-tts 重试")
            for ext in (".mp3", ".ogg", ".wav"):
                try:
                    os.remove(out.replace(".mp3", ext))
                except OSError:
                    pass
            retry = asyncio.run(
                generate_voice(
                    text, out, speaker=speaker, backend="edge", allow_fallback=True
                )
            )
            if _valid_audio(retry):
                return retry
    except Exception as e:
        print(f"TTS 失败[{index}]: {e}")
    # edge-tts 可能输出其他扩展名，兜底扫描
    for ext in (".mp3", ".ogg", ".wav"):
        cand = out.replace(".mp3", ext)
        if _valid_audio(cand):
            return cand
    return None


_ORIG_TTS_SEGMENT = _tts_segment


# 表情包 PiP 可落位（9:16竖屏下下半屏居中区域）
# 画布 1080x1920，下半屏 y=0.55~0.78（约1056px~1498px），避开底部字幕区
# 水平居中 x=0.3~0.7（约324px~756px），以居中为基准微偏移避免呆板
_STICKER_SPOTS = [
    (0.50, 0.62),  # 正中心偏上
    (0.50, 0.70),  # 正中心
    (0.38, 0.65),  # 居中偏左
    (0.62, 0.65),  # 居中偏右
    (0.42, 0.72),  # 左下偏中
    (0.58, 0.72),  # 右下偏中
    (0.50, 0.58),  # 中心偏上
    (0.35, 0.58),  # 左上偏中
    (0.65, 0.58),  # 右上偏中
]

# 贴纸入场动画风格（8种，随机选择）
_STICKER_ENTRY_STYLES = (
    "pop_bounce", "drop_bounce", "spin_zoom", "swing_in",
    "slide_right", "slide_left", "zoom_pulse", "flip_in"
)

# 贴纸循环闲置动画（入场后持续微动，增加生动感）
_STICKER_IDLE_ANIMS = ("none", "gentle_swing", "small_bounce", "heartbeat", "slow_zoom")


def _add_to_track_safe(project, add_fn, base_track: str, start_time: float, duration: float,
                       max_tracks: int = 5, gap: float = 0.05):
    """安全添加素材到轨道，遇到重叠自动切换到新轨道。

    Args:
        add_fn: callable(track_name) -> segment，执行实际的添加操作
        base_track: 基础轨道名
        start_time: 开始时间
        duration: 时长
        max_tracks: 最大轨道数
        gap: 轨道间安全间隙（秒）
    """
    for i in range(max_tracks):
        track_name = base_track if i == 0 else f"{base_track}{i+1}"
        try:
            return add_fn(track_name)
        except Exception as e:
            err_str = str(e)
            if "overlap" in err_str.lower() and i < max_tracks - 1:
                continue
            # 不是重叠错误或已达最大轨道数，打印警告并跳过
            print(f"添加到 {track_name} 失败: {str(e)[:80]}")
            return None
    return None


def _add_sticker_pip(project, sticker_path: str, start_time: float, duration: float,
                     sticker_info: dict = None):
    """表情包叠加为画中画：自适应尺寸 + 随机位置 + 丰富入场动画 + 闲置微动。

    Args:
        sticker_info: 来自 compute_sticker_scale() 的尺寸信息，None 则自动计算
    """
    try:
        # 计算贴纸自适应尺寸（提前计算，避免重复）
        if sticker_info is None:
            sticker_info = compute_sticker_scale(sticker_path)
        target_scale = sticker_info.get("uniform_scale", 0.26)
        px, py = random.choice(_STICKER_SPOTS)
        style = random.choice(_STICKER_ENTRY_STYLES)
        idle_style = random.choice(_STICKER_IDLE_ANIMS)

        def _do_add(track_name):
            return project.add_media_safe(
                sticker_path,
                start_time=_fmt_s(start_time),
                duration=_fmt_s(duration),
                track_name=track_name,
            )

        seg = _add_to_track_safe(project, _do_add, "StickerTrack", start_time, duration)
        if seg is None:
            return None
        from pyJianYingDraft import KeyframeProperty as KP

        t_start = int(start_time * 1e6)
        t_end = int((start_time + duration) * 1e6)
        entry_dur_us = int(0.18 * 1e6)  # 入场动画时长 0.18s
        t_entry_end = t_start + entry_dur_us

        # ---- 入场动画关键帧 ----
        if style == "pop_bounce":
            # 弹性缩放弹出：0 -> 1.15x -> 0.9x -> 1.0x
            seg.add_keyframe(KP.uniform_scale, t_start, 0.0)
            seg.add_keyframe(KP.uniform_scale, t_start + int(entry_dur_us * 0.5), target_scale * 1.18)
            seg.add_keyframe(KP.uniform_scale, t_start + int(entry_dur_us * 0.8), target_scale * 0.92)
            seg.add_keyframe(KP.uniform_scale, t_entry_end, target_scale)
            seg.add_keyframe(KP.position_x, t_start, px)
            seg.add_keyframe(KP.position_y, t_start, py)
            seg.add_keyframe(KP.rotation, t_start, -6)
            seg.add_keyframe(KP.rotation, t_entry_end, 0)

        elif style == "drop_bounce":
            # 从上方掉落 + 弹跳（从屏幕上半部分掉落到下半屏中心）
            drop_y = py - 0.35
            seg.add_keyframe(KP.position_y, t_start, drop_y)
            seg.add_keyframe(KP.position_y, t_start + int(entry_dur_us * 0.55), py + 0.04)
            seg.add_keyframe(KP.position_y, t_start + int(entry_dur_us * 0.75), py - 0.015)
            seg.add_keyframe(KP.position_y, t_entry_end, py)
            seg.add_keyframe(KP.position_x, t_start, px)
            seg.add_keyframe(KP.uniform_scale, t_start, target_scale * 0.6)
            seg.add_keyframe(KP.uniform_scale, t_entry_end, target_scale)
            seg.add_keyframe(KP.rotation, t_start, 12)
            seg.add_keyframe(KP.rotation, t_entry_end, 0)

        elif style == "spin_zoom":
            # 旋转 + 缩放入场
            seg.add_keyframe(KP.uniform_scale, t_start, 0.0)
            seg.add_keyframe(KP.uniform_scale, t_entry_end, target_scale)
            seg.add_keyframe(KP.rotation, t_start, 180)
            seg.add_keyframe(KP.rotation, t_entry_end, 0)
            seg.add_keyframe(KP.position_x, t_start, px)
            seg.add_keyframe(KP.position_y, t_start, py)

        elif style == "swing_in":
            # 钟摆摇摆入场
            seg.add_keyframe(KP.uniform_scale, t_start, 0.3)
            seg.add_keyframe(KP.uniform_scale, t_entry_end, target_scale)
            seg.add_keyframe(KP.rotation, t_start, -25)
            seg.add_keyframe(KP.rotation, t_start + int(entry_dur_us * 0.4), 15)
            seg.add_keyframe(KP.rotation, t_start + int(entry_dur_us * 0.7), -8)
            seg.add_keyframe(KP.rotation, t_entry_end, 0)
            seg.add_keyframe(KP.position_x, t_start, px - 0.05)
            seg.add_keyframe(KP.position_x, t_entry_end, px)
            seg.add_keyframe(KP.position_y, t_start, py)

        elif style == "slide_right":
            # 从右侧滑入
            slide_x = px + 0.45
            seg.add_keyframe(KP.position_x, t_start, slide_x)
            seg.add_keyframe(KP.position_x, t_entry_end, px)
            seg.add_keyframe(KP.position_y, t_start, py)
            seg.add_keyframe(KP.uniform_scale, t_start, target_scale * 0.6)
            seg.add_keyframe(KP.uniform_scale, t_start + int(entry_dur_us * 0.6), target_scale * 1.08)
            seg.add_keyframe(KP.uniform_scale, t_entry_end, target_scale)
            seg.add_keyframe(KP.rotation, t_start, -10)
            seg.add_keyframe(KP.rotation, t_entry_end, 0)

        elif style == "slide_left":
            # 从左侧滑入
            slide_x = px - 0.45
            seg.add_keyframe(KP.position_x, t_start, slide_x)
            seg.add_keyframe(KP.position_x, t_entry_end, px)
            seg.add_keyframe(KP.position_y, t_start, py)
            seg.add_keyframe(KP.uniform_scale, t_start, target_scale * 0.6)
            seg.add_keyframe(KP.uniform_scale, t_start + int(entry_dur_us * 0.6), target_scale * 1.08)
            seg.add_keyframe(KP.uniform_scale, t_entry_end, target_scale)
            seg.add_keyframe(KP.rotation, t_start, 10)
            seg.add_keyframe(KP.rotation, t_entry_end, 0)

        elif style == "zoom_pulse":
            # 脉冲缩放（心跳感）
            seg.add_keyframe(KP.uniform_scale, t_start, 0.0)
            seg.add_keyframe(KP.uniform_scale, t_start + int(entry_dur_us * 0.4), target_scale * 1.25)
            seg.add_keyframe(KP.uniform_scale, t_start + int(entry_dur_us * 0.7), target_scale * 0.88)
            seg.add_keyframe(KP.uniform_scale, t_entry_end, target_scale)
            seg.add_keyframe(KP.position_x, t_start, px)
            seg.add_keyframe(KP.position_y, t_start, py)

        else:  # flip_in
            # 翻转入场（Y轴旋转感，用 scale_x 模拟）
            seg.add_keyframe(KP.uniform_scale, t_start, target_scale * 0.1)
            seg.add_keyframe(KP.uniform_scale, t_entry_end, target_scale)
            seg.add_keyframe(KP.rotation, t_start, 30)
            seg.add_keyframe(KP.rotation, t_entry_end, 0)
            seg.add_keyframe(KP.position_x, t_start, px)
            seg.add_keyframe(KP.position_y, t_start, py - 0.1)
            seg.add_keyframe(KP.position_y, t_entry_end, py)

        # ---- 闲置微动动画（入场后）----
        if idle_style != "none" and duration > 0.5:
            idle_start = t_entry_end + int(0.1 * 1e6)
            idle_cycle = int(0.8 * 1e6)  # 微动周期 0.8s
            if idle_style == "gentle_swing":
                # 轻微左右摇摆
                for k in range(4):
                    t = idle_start + k * idle_cycle
                    if t >= t_end:
                        break
                    seg.add_keyframe(KP.rotation, t, -4 if k % 2 == 0 else 4)
            elif idle_style == "small_bounce":
                # 小幅度上下弹跳
                for k in range(4):
                    t = idle_start + k * idle_cycle
                    if t >= t_end:
                        break
                    seg.add_keyframe(KP.position_y, t, py - 0.015 if k % 2 == 0 else py)
                    seg.add_keyframe(KP.uniform_scale, t, target_scale * (1.04 if k % 2 == 0 else 1.0))
            elif idle_style == "heartbeat":
                # 心跳缩放
                for k in range(6):
                    t = idle_start + k * int(0.5 * 1e6)
                    if t >= t_end:
                        break
                    seg.add_keyframe(KP.uniform_scale, t, target_scale * (1.06 if k % 2 == 0 else 1.0))
            elif idle_style == "slow_zoom":
                # 缓慢呼吸缩放
                for k in range(3):
                    t = idle_start + k * int(1.0 * 1e6)
                    if t >= t_end:
                        break
                    seg.add_keyframe(KP.uniform_scale, t, target_scale * (1.05 if k % 2 == 0 else 0.98))

        # ---- 结束关键帧（保持到结束）----
        seg.add_keyframe(KP.uniform_scale, t_end, target_scale)
        seg.add_keyframe(KP.position_x, t_end, px)
        seg.add_keyframe(KP.position_y, t_end, py)
        seg.add_keyframe(KP.rotation, t_end, 0)
        return seg
    except Exception as e:
        print(f"表情包叠加失败: {e}")
        import traceback
        traceback.print_exc()
        return None


# 与聊天卡片（HTML 烘焙）会重复的「对话框」类贴纸（纯聊天气泡框），叠加无意义，跳过。
# 注意：仅按「对话框」精确过滤；"微信聊天记录表情包"这类梗图/表情包虽含"聊天记录"字样，
# 但本质是反应表情，应保留作为叠层贴纸（如「你别找茬」meme）。
_STICKER_DIALOG_SKIP = ("对话框", "聊天背景")


def _try_add_native_sticker(project, m: dict):
    """聊天表情包自动选贴：调用 jianying-editor 原生贴纸自动匹配，叠加到草稿。

    仅当 jianying-editor 的 JyProject 已混入 StickerOpsMixin（带 select_sticker_for_chat /
    add_sticker）时生效；否则静默跳过，不影响其它组装。贴纸按消息时间窗放置于
    竖屏下半区，并按角色左右分布，避免遮挡居中气泡。
    """
    if not NATIVE_STICKERS_ENABLED:
        return
    query = (m.get("text") or "").strip() or (m.get("emotion") or "")
    if not query:
        return

    sel = getattr(project, "select_sticker_for_chat", None)
    add = getattr(project, "add_sticker", None)
    if not callable(sel) or not callable(add):
        return  # 未接入贴纸能力，静默跳过

    try:
        picks = sel(query, top_k=1)
    except Exception as e:
        print(f"[warn] 原生贴纸自动匹配跳过: {e}")
        return
    if not picks:
        return

    info = picks[0]
    name = info.get("name", "")
    if not info.get("selectable") or not info.get("path"):
        return  # 占位/无真名的缓存贴纸（只能按 ID 添加）不叠加
    if any(k in name for k in _STICKER_DIALOG_SKIP):
        return  # 对话框类会和聊天卡片重复

    start = float(m.get("start_s", 0) or 0)
    end = float(m.get("end_s", start) or start)
    dur = max(end - start, 1.5)
    if dur > 4.0:
        dur = 4.0

    # 竖屏坐标：y>0 偏下（气泡居中，贴纸落下半区）；x 按角色左右分布
    role = str(m.get("role") or "").lower()
    tx = -0.30 if role in ("a", "我", "me", "user") else 0.30 if role in ("b", "你", "he", "she") else 0.0
    transform = {
        "alpha": 1.0,
        "rotation": 0.0,
        "scale_x": 0.5,
        "scale_y": 0.5,
        "transform_x": tx,
        "transform_y": 0.35,
    }
    try:
        add(info, start_time=f"{start:.1f}s", duration=f"{dur:.1f}s", transform=transform,
            track_name="StickerTrack")
    except Exception as e:
        print(f"[warn] 原生贴纸注入失败（跳过）: {e}")


def _add_subtitle(project, text: str, start_time: float, duration: float):
    """逐条消息字幕：底部 + 随机入场动画，自动处理轨道重叠。"""
    try:
        anim = random_text_animation()
        import pyJianYingDraft as draft

        def _do_add(track_name):
            return project.add_text_simple(
                text,
                start_time=_fmt_s(start_time),
                duration=_fmt_s(duration),
                track_name=track_name,
                anim_in=anim,
                clip_settings=draft.ClipSettings(transform_y=-0.82),
            )

        return _add_to_track_safe(project, _do_add, "Subtitles", start_time, duration)
    except Exception as e:
        print(f"字幕失败: {e}")
        return None


# ============================================================
# 阶段1：plan_builder —— 走 engine 流水线生成 plan（薄封装）
# ============================================================

def _apply_external_alignment(d, external_alignment: dict):
    """整曲模式：按外部对齐结果直接回填每条消息的时间点。

    保留外部（可能人工微调过）的 start_s/end_s，不重新 VAD 对齐；
    旁白按是否已包含在整曲中标记 manual（不自动 TTS）。
    缺失对齐信息的消息给 1.5s 兜底窗口。
    """
    alignment = external_alignment.get("alignment") or []
    align_map = {a.get("index"): a for a in alignment}
    narrations_in_audio = external_alignment.get("narrations_in_audio", False)
    manual_narrations = []
    for i, msg in enumerate(d.messages):
        a = align_map.get(i)
        if a is None:
            # 兜底：无对齐信息给 1.5s 窗口
            prev_end = d.messages[i - 1].audio.end_s if i > 0 else 0.5
            msg.audio.start_s = round(prev_end, 2)
            msg.audio.end_s = round(prev_end + 1.5, 2)
            msg.audio.source = "song"
            msg.audio.path = ""
            continue
        msg.audio.start_s = float(a.get("start_s", 0.0))
        msg.audio.end_s = float(a.get("end_s", msg.audio.start_s))
        msg.audio.source = "song"
        msg.audio.path = ""
        if msg.narration:
            # 旁白：在整曲中则用整曲，否则标记手工处理
            manual = bool(a.get("manual", False)) or not narrations_in_audio
            msg.audio.manual = manual
            if manual:
                manual_narrations.append(i)
        else:
            msg.audio.manual = bool(a.get("manual", False))

    d.meta["song_audio"] = external_alignment.get("full_audio") or ""
    d.meta["total_duration_s"] = float(external_alignment.get("total_duration", 0.0))
    d.meta["song_mode"] = True
    d.meta["narrations_in_audio"] = narrations_in_audio
    d.meta["alignment_method"] = external_alignment.get("alignment_method") or "manual"
    d.meta["manual_narrations"] = manual_narrations


def _inject_manual_audio_patch():
    """若外部模块 patch 了本模块的 _tts_segment/_speedup_audio，则把 patch
    版本桥接进 engine.audio_planner（签名由 3 参适配为 4 参）。

    场景：gospel_dialog 的逐句手动音频模式（patch_tts_with_manual）会把
    这两个函数替换为 _patched_tts/_no_speedup；薄封装流水线的逐句 TTS 由
    engine.audio_planner 完成，必须让 patch 生效。
    """
    if _tts_segment is _ORIG_TTS_SEGMENT and _speedup_audio is _ORIG_SPEEDUP_AUDIO:
        return
    import engine.audio_planner as ap
    if _tts_segment is not _ORIG_TTS_SEGMENT:
        _orig = _tts_segment

        def _bridged_tts(text, speaker, index, _tts_dir=""):
            return _orig(text, speaker, index)

        ap._tts_segment = _bridged_tts
    if _speedup_audio is not _ORIG_SPEEDUP_AUDIO:
        _orig_sp = _speedup_audio

        def _bridged_speedup(audio_path, factor, index, _tts_dir=""):
            return _orig_sp(audio_path, factor, index)

        ap._speedup_audio = _bridged_speedup


def build_plan(script: dict, speaker: str, plan_path: str = None,
               speedup: float = DEFAULT_SPEEDUP,
               external_alignment: dict = None) -> dict:
    """阶段1：经 engine 流水线生成 plan 并落盘（薄封装版）。

    plan.json 是中间产物，人工可审核/修改：
      - sfx.effect_id 改成人工在剪映找到的素材 id
      - sticker_path 换成真实贴纸路径
      - missing_assets 提示待补素材
    修改后重新执行（--plan plan.json）即用人工值组装。

    多角色音色支持：script 中 role_speakers 字段可映射 role -> speaker_id。

    external_alignment: 外部时间轴对齐结果（整曲模式，来自 audio_aligner）
      传入时跳过逐句 TTS，直接使用外部检测的时间点（保留人工微调），
      仅对旁白标记 manual；plan 中增加 song_audio/song_mode 字段。
    """
    messages = script.get("messages", [])
    if not messages:
        raise ValueError("剧本没有消息")

    use_external = external_alignment is not None

    # ---- 1. 构造 Dialogue（旧剧本 dict -> 运行时对象） ----
    from engine.models import Dialogue
    d = Dialogue.from_legacy_script(script)
    d.speaker = speaker or DEFAULT_SPEAKER
    d.speedup = speedup
    d.meta["speaker"] = speaker or DEFAULT_SPEAKER
    d.meta["speedup"] = speedup

    # 兼容旧剧本显式字段，并重算音色（对齐旧 _get_speaker_for_role 逻辑）
    raw_msgs = script.get("messages", [])
    for m, raw in zip(d.messages, raw_msgs):
        # 音色：role_speakers 优先，否则全局 speaker
        m.audio.voice = d.role_speakers.get(m.role) or d.speaker
        # 旧剧本可能用 "sticker" 字段指显式贴纸（engine 读取 image 字段）
        if not m.image and raw.get("sticker"):
            m.image = raw["sticker"]
        # 旧剧本显式 sfx（字符串或 dict）映射进 visual.sfx
        raw_sfx = raw.get("sfx")
        if raw_sfx:
            if isinstance(raw_sfx, dict):
                m.visual.sfx = dict(raw_sfx)
            else:
                m.visual.sfx = {"effect_id": str(raw_sfx), "title": "剧本指定", "source": "manual"}

    # ---- 2. 组装 Pipeline 执行到 TimelineNode 为止 ----
    from engine.models import PipelineContext
    from engine.pipeline import build_default_pipeline

    ctx = PipelineContext(source={
        "speedup": speedup,
        "audio_mode": "auto",
        "tts_dir": TTS_DIR,
    })
    ctx.dialogue = d
    pipe = build_default_pipeline(render_enabled=False, assemble_enabled=False)

    if use_external:
        # 整曲模式：直接回填外部对齐时间点，跳过 audio/align 节点
        _apply_external_alignment(d, external_alignment)
        ctx = pipe.execute(ctx, from_node="visual_planner")
    else:
        # 正常模式：逐句 TTS（align 节点因 alignment_method=tts_timings 自动跳过）
        _inject_manual_audio_patch()
        ctx = pipe.execute(ctx, from_node="audio_planner")

    if ctx.timeline is None:
        raise RuntimeError("流水线未生成时间轴: " + "; ".join(ctx.errors or []))

    # ---- 3. Timeline -> 旧格式 plan（schema_version=4）落盘 ----
    from engine.timeline_planner import timeline_to_legacy_plan
    plan = timeline_to_legacy_plan(ctx.timeline)

    # 对齐旧 plan：资产积分预算取实际使用量
    est = plan.get("estimates", {})
    if est:
        est["asset_points_total"] = est.get("asset_points_used", 0)

    if plan_path:
        os.makedirs(os.path.dirname(os.path.abspath(plan_path)), exist_ok=True)
        with open(plan_path, "w", encoding="utf-8") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        print("plan 已生成: " + plan_path)
        if plan.get("missing_assets"):
            print(f"警告: 发现 {len(plan['missing_assets'])} 处素材缺失，"
                  "清单见 plan.json 的 missing_assets")
    return plan


# ============================================================
# 阶段3：assemble —— 按 plan 组装剪映草稿（原样保留）
# ============================================================

def _assemble_draft(plan: dict, draft_root: str = None,
                    export_video: str = None, asset_dir: str = None) -> dict:
    """阶段3：按 plan 组装剪映草稿并保存。缺失素材不凑合，仅保留清单提示。

    注意：本函数被 engine/pipeline.py 的 AssembleNode 运行时 import，
    以及 build_gospel_video 直接调用；消费旧格式 plan dict（timeline_to_legacy_plan 产出）。
    """
    project_name = plan["project_name"]
    resolution = plan.get("resolution", "1080")
    width, height = (CANVAS_WIDTH, CANVAS_HEIGHT) if str(resolution) == "1080" else (CANVAS_HEIGHT, CANVAS_WIDTH)
    plan_messages = plan["messages"]
    total_duration = plan["estimates"]["total_duration_s"]
    errors: list = []
    missing_assets: list = plan.get("missing_assets", [])

    # ---- 3a. 渲染聊天视频（阶段2，与编排共用时序） ----
    os.makedirs(CHAT_VIDEO_DIR, exist_ok=True)
    chat_video = os.path.join(CHAT_VIDEO_DIR, f"{project_name}_chat.webm")
    timings = [(m["start_s"], m["end_s"]) for m in plan_messages]
    try:
        ok = render_chat_scene(
            {"title": plan.get("title"), "messages": [
                {"role": m["role"], "type": m["type"], "text": m.get("text"),
                 "image": m.get("sticker_path"),
                 "sticker": m.get("overlay_sticker"),
                 "emotion": m.get("emotion"),
                 "is_narration": m.get("is_narration", False) or m["role"] in ("旁白", "narrator", "解说", "画外音")}
                for m in plan_messages
            ]},
            chat_video, width=width, height=height, msg_timings=timings,
            speedup=plan.get("speedup", 1.0),
            total_duration=total_duration,
            asset_dir=asset_dir,
        )
        if not ok:
            return {"ok": False, "project_name": project_name, "draft_path": None,
                    "chat_video": None, "errors": ["聊天视频渲染失败"],
                    "missing_assets": missing_assets}
    except Exception as e:
        return {"ok": False, "project_name": project_name, "draft_path": None,
                "chat_video": None, "errors": [f"聊天视频渲染异常: {e}"],
                "missing_assets": missing_assets}

    # ---- 3b. 组装草稿 ----
    try:
        from jy_wrapper import JyProject

        project = JyProject(project_name, width=width, height=height,
                            drafts_root=draft_root, overwrite=True)
        chat_seg = project.add_media_safe(
            chat_video, start_time="0s", duration=_fmt_s(total_duration),
            track_name="VideoTrack",
        )

        # 整曲模式：添加整首歌曲作为主音轨
        song_mode = plan.get("song_mode", False)
        song_audio = plan.get("song_audio")
        if song_mode and song_audio and os.path.isfile(song_audio):
            project.add_audio_safe(
                song_audio, start_time="0s",
                duration=_fmt_s(total_duration), track_name="SongTrack",
            )

        # 逐条配音（整曲模式下只有旁白TTS；正常模式下全部配音）
        for m in plan_messages:
            if m.get("type") == "sticker" or not m.get("voice"):
                continue
            project.add_audio_safe(
                m["voice"], start_time=_fmt_s(m["start_s"]),
                duration=_fmt_s(m["duration_s"]), track_name="VoiceTrack",
            )

        # 字幕 + 音效（贴纸已在HTML卡片下方渲染，不再PiP叠加）
        for m in plan_messages:
            start, end = m["start_s"], m["end_s"]
            # 跳过旁白（已在聊天视频中以居中浮层渲染，避免底部字幕重复）和零时长消息（避免退化字幕段）
            if m.get("type") == "text" and m.get("text") and not m.get("is_narration") and end > start:
                _add_subtitle(project, m["text"], start, end - start)
            # 音效（plan 中人工填好的 effect_id 优先）
            if m.get("sfx") and m["sfx"].get("effect_id"):
                try:
                    project.add_cloud_media(
                        str(m["sfx"]["effect_id"]), start_time=_fmt_s(start),
                        track_name="SFX_Track",
                    )
                except Exception as e:
                    missing_assets.append({
                        "type": "sfx", "index": m["index"],
                        "emotion": m.get("emotion"),
                        "detail": f"音效 {m['sfx'].get('effect_id')} 插入异常: {e}",
                        "hint": "请人工在剪映音频素材库中插入",
                    })

            # 画面特效（[[特效:...]] 标记归一化后的剪映 identifier）
            se = (m.get("scene_effect") or "").strip()
            if se and end > start:
                try:
                    project.add_effect_simple(
                        se, start_time=_fmt_s(start),
                        duration=_fmt_s(end - start), track_name="EffectTrack",
                    )
                except Exception as e:
                    missing_assets.append({
                        "type": "scene_effect", "index": m["index"],
                        "emotion": m.get("emotion"),
                        "detail": f"画面特效「{se}」插入异常: {e}",
                        "hint": "请人工在剪映素材库中搜索该模板并手动套用",
                    })

            # 剪映原生贴纸自动匹配（聊天表情包）：按消息文本/情绪选贴并叠加
            if NATIVE_STICKERS_ENABLED and end > start:
                _try_add_native_sticker(project, m)

        # 随机转场（容错）
        try:
            if chat_seg is not None:
                project.add_transition_simple(
                    random_transition(), video_segment=chat_seg, duration="0.8s"
                )
        except Exception as e:
            print(f"转场失败: {e}")

        # BGM（整曲模式下不加额外BGM，整曲本身就是歌曲）
        bgm_query = plan.get("bgm_query")
        if bgm_query and not song_mode:
            try:
                bgm = project.add_cloud_music(
                    bgm_query, start_time="0s", duration=_fmt_s(total_duration)
                )
                if bgm is not None:
                    bgm.volume = BGM_VOLUME
            except Exception as e:
                print(f"BGM 失败: {e}")

        project.save()
        draft_path = project.draft_dir if hasattr(project, "draft_dir") else None
    except Exception as e:
        return {"ok": False, "project_name": project_name, "draft_path": None,
                "chat_video": chat_video, "errors": [f"草稿组装失败: {e}"],
                "missing_assets": missing_assets}

    # ---- 3c. 可选导出 ----
    if export_video:
        try:
            from auto_exporter import auto_export
            ok_exp = auto_export(project_name, export_video,
                                 resolution=int(width), framerate=30)
            if not ok_exp:
                errors.append("导出失败（请确认剪映已打开）")
        except Exception as e:
            errors.append(f"导出异常: {e}")

    return {"ok": True, "project_name": project_name, "draft_path": draft_path,
            "chat_video": chat_video, "errors": errors,
            "missing_assets": missing_assets}


def build_gospel_video(script_path: str, plan_path: str = None, draft_root: str = None,
                       export_video: str = None, force_speaker: str = None,
                       plan_only: bool = False,
                       speedup: float = DEFAULT_SPEEDUP,
                       external_alignment: dict = None,
                       force_rebuild_plan: bool = False,
                       asset_dir: str = None) -> dict:
    """完整流水线入口（薄封装：build_plan 内部走 engine 流水线）。

    Args:
        script_path: 剧本 JSON 路径
        plan_path: plan.json 路径。None 则自动生成到 assets/plan.json；
                   指定已存在的文件则跳过配音/匹配，直接用人工修改过的映射。
        draft_root: 剪映草稿根（None 用 skill 默认）
        export_video: 导出视频路径（None 不导出；需剪映开着）
        force_speaker: 覆盖剧本 speaker
        plan_only: 只执行阶段1+2（plan + 聊天视频），不组装草稿
        speedup: 全局倍速（整曲模式下不影响音频速度，仅影响动画节奏）
        external_alignment: 外部时间轴对齐结果（整曲模式，来自 audio_aligner）
        force_rebuild_plan: 强制重新生成plan（即使plan_path已存在），用于自动化流程

    Returns:
        {"ok", "project_name", "draft_path", "chat_video", "errors", "missing_assets"}
    """
    try:
        script = _load_script(script_path)
    except Exception as e:
        return {"ok": False, "project_name": None, "draft_path": None,
                "chat_video": None, "errors": [f"剧本加载失败: {e}"],
                "missing_assets": []}

    project_name = (script.get("project_name") or script.get("title")
                    or "GospelVideo")
    speaker = force_speaker or script.get("speaker") or DEFAULT_SPEAKER
    if not script.get("messages"):
        return {"ok": False, "project_name": None, "draft_path": None,
                "chat_video": None, "errors": ["剧本没有消息"], "missing_assets": []}

    # ---- 阶段1：生成或复用 plan ----
    can_reuse = (plan_path and os.path.exists(plan_path)
                 and not force_rebuild_plan
                 and external_alignment is None)
    if can_reuse:
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                existing_plan = json.load(f)
            # 验证plan是否匹配当前项目
            if existing_plan.get("project_name") == project_name:
                plan = existing_plan
                print(f"复用已有 plan: {plan_path}")
                plan_speed = plan.get("speedup", 1.0)
                if abs(plan_speed - speedup) > 1e-6:
                    print(f"警告: 该 plan 倍速为 {plan_speed}，与当前 --speed {speedup} "
                          "不一致，重新生成 plan")
                    plan = build_plan(script, speaker, plan_path or DEFAULT_PLAN,
                                      speedup=speedup, external_alignment=external_alignment)
            else:
                print(f"已有 plan 属于项目 '{existing_plan.get('project_name')}'，"
                      f"不匹配当前项目 '{project_name}'，重新生成")
                plan = build_plan(script, speaker, plan_path or DEFAULT_PLAN,
                                  speedup=speedup, external_alignment=external_alignment)
        except Exception as e:
            return {"ok": False, "project_name": project_name, "draft_path": None,
                    "chat_video": None, "errors": [f"plan 加载失败: {e}"],
                    "missing_assets": []}
    else:
        plan = build_plan(script, speaker, plan_path or DEFAULT_PLAN, speedup=speedup,
                          external_alignment=external_alignment)

    missing_assets = plan.get("missing_assets", [])

    if plan_only:
        # 阶段2 只渲染聊天视频（供人工审核素材匹配/补素材），不组装草稿
        from chat_scene_renderer import render_chat_scene
        resolution = plan.get("resolution", "1080")
        width, height = (CANVAS_WIDTH, CANVAS_HEIGHT) if str(resolution) == "1080" else (CANVAS_HEIGHT, CANVAS_WIDTH)
        os.makedirs(CHAT_VIDEO_DIR, exist_ok=True)
        chat_video = os.path.join(CHAT_VIDEO_DIR, f"{project_name}_chat.webm")
        timings = [(m["start_s"], m["end_s"]) for m in plan["messages"]]
        ok = render_chat_scene(
            {"title": plan.get("title"), "messages": [
                {"role": m["role"], "type": m["type"], "text": m.get("text"),
                 "image": m.get("sticker_path"),
                 "sticker": m.get("overlay_sticker"),
                 "emotion": m.get("emotion"),
                 "is_narration": m.get("is_narration", False) or m["role"] in ("旁白", "narrator", "解说", "画外音")}
                for m in plan["messages"]
            ]},
            chat_video, width=width, height=height, msg_timings=timings,
            speedup=plan.get("speedup", 1.0),
            total_duration=plan["estimates"]["total_duration_s"],
            asset_dir=asset_dir,
        )
        if not ok:
            return {"ok": False, "project_name": project_name, "draft_path": None,
                    "chat_video": None, "errors": ["聊天视频渲染失败"],
                    "missing_assets": missing_assets}
        return {"ok": True, "project_name": project_name, "draft_path": None,
                "chat_video": chat_video, "errors": [],
                "missing_assets": missing_assets}

    # 阶段2 渲染 + 阶段3 组装
    assembled = _assemble_draft(plan, draft_root=draft_root, export_video=export_video,
                                asset_dir=asset_dir)
    assembled["missing_assets"] = missing_assets
    return assembled


def _print_report(r: dict, plan_only: bool = False):
    """人类可读的结果报告 + 素材缺失提示"""
    if r["ok"]:
        if plan_only:
            print("[OK] plan + 聊天视频已生成（草稿未组装，可先审核素材匹配）")
        else:
            print(f"[OK] 完成：草稿 {r['project_name']} 已保存")
        if r["draft_path"]:
            print(f"草稿路径: {r['draft_path']}")
        if r["chat_video"]:
            print(f"聊天视频: {r['chat_video']}")
    else:
        print("[FAIL] 失败：")
        for e in r["errors"]:
            print(f"  - {e}")

    missing = r.get("missing_assets", [])
    if missing:
        print(f"警告: 有 {len(missing)} 处素材缺失，以下位置需要人工去剪映素材库补素材：")
        for item in missing:
            kind = "音效" if item["type"] == "sfx" else "贴纸"
            print(f"  [{kind}] #消息{item.get('index')} [{item.get('emotion')}]")
            print(f"      问题: {item.get('detail')}")
            print(f"      建议: {item.get('hint')}")
    elif r["ok"]:
        print("素材全部匹配，无缺失。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="福音吐槽视频一键流水线（plan.json 中间产物版）")
    parser.add_argument("script", help="剧本 JSON 路径")
    parser.add_argument("--plan", default=None, help="plan.json 路径（默认 assets/plan.json；已存在则复用人工修改）")
    parser.add_argument("--plan-only", action="store_true", help="只生成 plan + 聊天视频，不组装草稿（供人工审核/补素材）")
    parser.add_argument("--export", default=None, help="导出视频路径（需剪映开着）")
    parser.add_argument("--speaker", default=None, help="覆盖配音人")
    parser.add_argument("--draft-root", default=None, help="剪映草稿根目录")
    parser.add_argument("--speed", type=float, default=DEFAULT_SPEEDUP,
                        help=f"整体节奏倍速（默认 {DEFAULT_SPEEDUP}，>1 更快：配音变速+时序压缩）")
    args = parser.parse_args()

    t0 = time.time()
    result = build_gospel_video(
        args.script, plan_path=args.plan, draft_root=args.draft_root,
        export_video=args.export, force_speaker=args.speaker,
        plan_only=args.plan_only, speedup=args.speed,
    )
    _print_report(result, plan_only=args.plan_only)
    print(f"\n耗时 {time.time() - t0:.1f}s")
    if not result["ok"]:
        sys.exit(1)
