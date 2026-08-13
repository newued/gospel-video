# -*- coding: utf-8 -*-
"""音频规划器（v5 架构）—— 为每条消息回填 AudioClip。

三种模式：
  - auto：逐句 TTS（走 CACHE 缓存复用），残缺重试 edge-tts，可选变速，
          然后计算时序回填 start/end
  - manual：整曲优先（find_full_song_audio -> align_song，整曲写入 meta.song_audio）；
            无整曲则逐句映射 custom_dir 下的序号音频（000.mp3/voice_000.mp3/a0.mp3），
            缺失回退 TTS，映射到的消息源标记 manual
  - suno：仅生成提示词（在别处），本节点不处理

TTS / 变速的底层调用方式与 gospel_automator 现实现保持一致
（universal_tts.generate_voice / ffmpeg atempo）。
"""
from __future__ import annotations

import asyncio
import glob
import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

from .cache import CACHE
from .models import Dialogue, Message
from .alignment import (
    AlignmentEngine,
    find_full_song_audio,
    get_audio_duration,
)

DEFAULT_SPEAKER = "zh_male_huoli"
MSG_GAP_S = 0.4          # 消息间节奏留白（秒）
FIRST_MSG_OFFSET_S = 0.5  # 首条消息延迟（秒）
MIN_TTS_BYTES = 500      # 小于此字节数视为残缺/无效音频
TAIL_PADDING_S = 1.5     # 结尾缓冲（秒）


# ---------------------------------------------------------------------------
# 路径定位（与 gospel_automator 一致）
# ---------------------------------------------------------------------------

def _locate_jy_skill() -> str:
    """定位 jianying-editor skill 根目录：环境变量 JY_SKILL_ROOT 优先，兜底常见安装位置。"""
    env = os.environ.get("JY_SKILL_ROOT")
    if env and os.path.isdir(os.path.join(env, "scripts")):
        return env
    for cand in (
        os.path.expanduser(r"~/.config/opencode/skills/jianying-editor"),
        os.path.expanduser(r"~/.agents/skills/jianying-editor"),
    ):
        if os.path.isdir(os.path.join(cand, "scripts")):
            return cand
    return env or ""


_SKILL_ROOT = _locate_jy_skill()
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SKILL_ROOT:
    _JY_SCRIPTS = os.path.join(_SKILL_ROOT, "scripts")
    if _JY_SCRIPTS not in sys.path:
        sys.path.insert(0, _JY_SCRIPTS)
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

# universal_tts / utils 依赖 jianying-editor 的 scripts，统一延迟到函数内导入，避免模块导入即崩溃
try:
    from utils.formatters import get_duration_ffprobe_cached as _ffprobe_dur
except Exception:
    _ffprobe_dur = None

_DEFAULT_TTS_DIR = os.path.join(_PROJECT_ROOT, "assets", "tts")


# ---------------------------------------------------------------------------
# 基础工具（与 gospel_automator 实现一致）
# ---------------------------------------------------------------------------

def _valid_audio(path: Optional[str]) -> bool:
    """校验音频文件有效（存在且体积足够，避免残缺文件混入草稿）。"""
    if not path or not os.path.exists(path):
        return False
    try:
        return os.path.getsize(path) >= MIN_TTS_BYTES
    except OSError:
        return False


def _find_ffmpeg() -> Optional[str]:
    """定位 ffmpeg：优先 imageio-ffmpeg，其次系统 PATH。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    return None


def _measure_duration(media_path: str) -> float:
    """返回媒体时长（秒），失败返回兜底 2.0s。"""
    if _ffprobe_dur is not None:
        try:
            dur = _ffprobe_dur(media_path)
            return dur if dur and dur > 0 else 2.0
        except Exception:
            pass
    try:
        dur = get_audio_duration(media_path)
        return dur if dur and dur > 0 else 2.0
    except Exception:
        return 2.0


# ---------------------------------------------------------------------------
# TTS / 变速（复制自 gospel_automator._tts_segment / _speedup_audio）
# ---------------------------------------------------------------------------

def _tts_segment(text: str, speaker: str, index: int, tts_dir: str) -> Optional[str]:
    """为单条消息生成配音，返回音频路径或 None；残缺文件自动删除并重试。

    调用方式与 gospel_automator 完全一致：
      generate_voice(text, out, speaker=speaker, allow_fallback=True)
      失败后以 backend="edge" 重试。
    """
    try:
        from universal_tts import generate_voice
    except Exception as e:
        print(f" universal_tts 不可用: {e}")
        return None

    os.makedirs(tts_dir, exist_ok=True)
    out = os.path.join(tts_dir, f"voice_{index:03d}.mp3")
    try:
        result = asyncio.run(
            generate_voice(text, out, speaker=speaker, allow_fallback=True)
        )
        if _valid_audio(result):
            return result
        if result:
            # SAMI 静默失败可能产出残缺文件（如 172B 空 OGG），清理后强制 edge-tts 重试
            print(f" TTS[{index}] 输出文件无效（{os.path.getsize(result)} 字节），改用 edge-tts 重试")
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
        print(f" TTS 失败[{index}]: {e}")
    # edge-tts 可能输出其他扩展名，兜底扫描
    for ext in (".mp3", ".ogg", ".wav"):
        cand = out.replace(".mp3", ext)
        if _valid_audio(cand):
            return cand
    return None


def _speedup_audio(audio_path: str, factor: float, index: int, tts_dir: str) -> str:
    """用 ffmpeg atempo 对配音变速（音调不变），输出 voice_NNN_x{factor}.mp3。

    变速失败/ffmpeg 缺失时返回原路径（容错，节奏略慢但不中断流水线）。
    """
    if not audio_path or factor <= 1.0001 or not _valid_audio(audio_path):
        return audio_path
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        print(" ffmpeg 不可用，跳过配音变速（--speed 不生效，按原速继续）")
        return audio_path
    out = os.path.join(tts_dir, f"voice_{index:03d}_x{factor:g}.mp3")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
           "-i", audio_path, "-filter:a", f"atempo={factor}",
           "-c:a", "libmp3lame", "-q:a", "2", out]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=120)
        if proc.returncode == 0 and _valid_audio(out):
            print(f" 配音[{index}] 变速 {factor}×: {os.path.basename(out)}")
            return out
        print(f" 配音[{index}] 变速失败，使用原速")
    except Exception as e:
        print(f" 配音[{index}] 变速异常: {e}")
    return audio_path


# ---------------------------------------------------------------------------
# 时序计算（复制自 gospel_automator._compute_timings）
# ---------------------------------------------------------------------------

def _compute_timings(messages: List[Message], voice_files: List[Optional[str]],
                     speedup: float = 1.0) -> List[Tuple[float, float]]:
    """计算每条消息的 (start_time_s, end_time_s)，与渲染/配音对齐。

    speedup > 1 时所有节奏参数（间隔/首条偏移/兜底时长）同步压缩。
    节奏：同一个人连续说话时消息紧凑（gap 减半）；不同人切换时逐条分明。
    """
    timings = []
    gap = MSG_GAP_S / speedup
    same_role_gap = gap * 0.5  # 同人连续说话：紧凑
    offset = FIRST_MSG_OFFSET_S / speedup
    cursor = offset
    prev_role = None
    for i, msg in enumerate(messages):
        if msg.type == "sticker":
            dur = 1.2 / speedup  # 表情包消息固定展示时长
        else:
            dur = _measure_duration(voice_files[i]) if voice_files[i] else 1.5 / speedup
        role = msg.role or "匿名"
        # 首条无间隔；后续按 同人连续/换人 取不同 gap
        if i > 0:
            cursor += same_role_gap if (prev_role == role) else gap
        timings.append((cursor, cursor + dur))
        cursor += dur
        prev_role = role
    return timings


# ---------------------------------------------------------------------------
# 内部辅助
# ---------------------------------------------------------------------------

def _resolve_speaker(dialogue: Dialogue, msg: Message) -> str:
    """解析单条消息的音色：message.audio.voice 优先，其次 role_speakers，最后全局默认。"""
    if msg.audio.voice:
        return msg.audio.voice
    if dialogue.role_speakers.get(msg.role):
        return dialogue.role_speakers[msg.role]
    return dialogue.speaker or DEFAULT_SPEAKER


def _scan_manual_audio(custom_dir: str, n_msgs: int) -> Dict[int, str]:
    """扫描手动音频目录，返回 {index: source_path}。

    命名支持：000.mp3 / voice_000.mp3 / a0.mp3 等，按第一个数字匹配消息序号。
    """
    mapping = {}
    if not os.path.isdir(custom_dir):
        return mapping
    exts = ("*.mp3", "*.wav", "*.ogg", "*.m4a", "*.aac")
    files = []
    for ext in exts:
        files.extend(glob.glob(os.path.join(custom_dir, ext)))
    for fpath in sorted(files):
        nums = re.findall(r"(\d+)", os.path.basename(fpath))
        if nums:
            idx = int(nums[0])
            if 0 <= idx < n_msgs:
                mapping[idx] = fpath
    return mapping


def _prepare_manual_audio(src: str, index: int, tts_dir: str) -> str:
    """把手动音频复制/转换为 tts_dir/voice_{index:03d}.mp3，返回目标路径。"""
    os.makedirs(tts_dir, exist_ok=True)
    dst = os.path.join(tts_dir, f"voice_{index:03d}.mp3")
    ext = os.path.splitext(src)[1].lower()
    if os.path.abspath(src) == os.path.abspath(dst):
        return dst
    if ext == ".mp3":
        shutil.copy2(src, dst)
    else:
        ffmpeg = _find_ffmpeg()
        if not ffmpeg:
            return src
        try:
            subprocess.run(
                [ffmpeg, "-y", "-i", src, "-acodec", "libmp3lame", "-q:a", "2", dst],
                capture_output=True, timeout=30,
            )
        except Exception as e:
            print(f" 手动音频转换失败[{index}]: {e}")
            return src
    return dst if os.path.isfile(dst) else src


# ---------------------------------------------------------------------------
# 各模式实现
# ---------------------------------------------------------------------------

def _plan_auto(dialogue: Dialogue, tts_dir: str, speedup: float) -> None:
    """auto 模式：逐句 TTS + 变速 + 时序回填。"""
    msgs = dialogue.messages
    voice_files: List[Optional[str]] = []
    for i, msg in enumerate(msgs):
        if msg.type == "sticker" or not (msg.text or "").strip():
            voice_files.append(None)
            continue
        speaker = _resolve_speaker(dialogue, msg)
        msg.audio.voice = speaker

        # 1. 走 CACHE：命中直接复用
        cached = CACHE.get_tts(msg.text, speaker)
        if cached:
            src = cached
        else:
            gen = _tts_segment(msg.text, speaker, i, tts_dir)
            if gen:
                try:
                    CACHE.put_tts(msg.text, speaker, gen)
                except Exception:
                    pass
                src = CACHE.get_tts(msg.text, speaker) or gen
            else:
                src = None

        if src:
            msg.audio.source = "tts"
            # 2. 变速（speedup != 1 时，走 CACHE.get_speedup）
            if speedup != 1.0:
                sp = CACHE.get_speedup(src, speedup)
                if sp:
                    msg.audio.path = sp
                    msg.audio.original = src
                    msg.audio.speedup = speedup
                else:
                    out = _speedup_audio(src, speedup, i, tts_dir)
                    if out != src:
                        try:
                            CACHE.put_speedup(src, speedup, out)
                        except Exception:
                            pass
                        msg.audio.path = CACHE.get_speedup(src, speedup) or out
                        msg.audio.original = src
                        msg.audio.speedup = speedup
                    else:
                        msg.audio.path = src
            else:
                msg.audio.path = src
        else:
            # TTS 完全失败：标记人工处理，无音频
            msg.audio.source = "none"
            msg.audio.manual = True

        voice_files.append(msg.audio.path or None)

    total = _backfill_timings(msgs, voice_files, speedup)
    dialogue.meta["total_duration_s"] = round(total, 2)
    dialogue.meta["alignment_method"] = "tts_timings"


def _plan_manual(dialogue: Dialogue, custom_dir: str, tts_dir: str,
                 speedup: float) -> None:
    """manual 模式：整曲优先；无整曲则逐句映射，缺失回退 TTS。"""
    msgs = dialogue.messages

    # 1. 整曲优先（Suno/妙响完整歌曲 -> VAD 整曲对齐）
    full_song = find_full_song_audio(custom_dir) if custom_dir else None
    if full_song:
        print(f" 检测到整曲音频: {full_song}，执行整曲对齐")
        try:
            engine = AlignmentEngine(mode="VAD")
            engine.align_song(dialogue, full_song)
            dialogue.meta["song_audio"] = os.path.abspath(full_song)
            dialogue.meta["song_mode"] = True
            # 整曲模式下歌词消息不生成逐句音频；旁白不在音频中时保持 manual 占位
            for msg in msgs:
                if msg.audio.source != "song":
                    msg.audio.source = "song"
            return
        except Exception as e:
            print(f" 整曲对齐失败: {e}，回退到逐句映射")
            for msg in msgs:
                msg.audio.start_s = 0.0
                msg.audio.end_s = 0.0
                msg.audio.source = "none"

    # 2. 逐句映射 custom_dir 下的序号音频
    mapping = _scan_manual_audio(custom_dir, len(msgs)) if custom_dir else {}
    voice_files: List[Optional[str]] = []
    for i, msg in enumerate(msgs):
        if msg.type == "sticker" or not (msg.text or "").strip():
            voice_files.append(None)
            continue
        src = mapping.get(i)
        if src:
            # 手动音频：复制到 tts_dir 统一命名，不做变速，源标记 manual
            dst = _prepare_manual_audio(src, i, tts_dir)
            msg.audio.path = dst or src
            msg.audio.source = "manual"
            msg.audio.original = src
            voice_files.append(msg.audio.path)
        else:
            # 缺失回退 TTS
            speaker = _resolve_speaker(dialogue, msg)
            msg.audio.voice = speaker
            cached = CACHE.get_tts(msg.text, speaker)
            if cached:
                msg.audio.path = cached
                msg.audio.source = "tts"
            else:
                gen = _tts_segment(msg.text, speaker, i, tts_dir)
                if gen:
                    try:
                        CACHE.put_tts(msg.text, speaker, gen)
                    except Exception:
                        pass
                    msg.audio.path = CACHE.get_tts(msg.text, speaker) or gen
                    msg.audio.source = "tts"
                else:
                    msg.audio.source = "none"
                    msg.audio.manual = True
            voice_files.append(msg.audio.path or None)

    # 手动音频不做变速，时序按原速计算
    total = _backfill_timings(msgs, voice_files, 1.0)
    dialogue.meta["total_duration_s"] = round(total, 2)
    dialogue.meta["alignment_method"] = "manual_timings"


def _backfill_timings(msgs: List[Message], voice_files: List[Optional[str]],
                      speedup: float) -> None:
    """按 _compute_timings 回填每条消息的 start/end/duration。"""
    timings = _compute_timings(msgs, voice_files, speedup)
    for msg, (s, e) in zip(msgs, timings):
        msg.audio.start_s = round(s, 2)
        msg.audio.end_s = round(e, 2)
        msg.audio.duration_s = round(e - s, 2)
    dialogue_total = timings[-1][1] + TAIL_PADDING_S / speedup if timings else 0.0
    return dialogue_total


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------

def plan_audio(dialogue: Dialogue, mode: str = "auto", custom_dir: str = "",
               tts_dir: str = "", speedup: float = 1.3) -> Dialogue:
    """音频规划：为每条消息回填 audio（path/duration/start/end/source/speedup）。

    Args:
        dialogue: Dialogue 对象（回填后直接返回同一对象）
        mode: auto（逐句 TTS）| manual（整曲优先/逐句映射）
        custom_dir: 手动音频目录（manual 模式用）
        tts_dir: TTS 输出目录（默认 <项目>/assets/tts）
        speedup: 全局倍速（>1 更快；manual 整曲模式不改变音频本身）
    """
    if not dialogue.messages:
        raise ValueError("对话没有消息")

    mode = (mode or "auto").lower()
    if mode not in ("auto", "manual"):
        raise ValueError(f"未知音频模式: {mode}（支持 auto / manual）")

    if not tts_dir:
        tts_dir = _DEFAULT_TTS_DIR
    os.makedirs(tts_dir, exist_ok=True)

    dialogue.speedup = speedup

    if mode == "manual":
        _plan_manual(dialogue, custom_dir, tts_dir, speedup)
    else:
        _plan_auto(dialogue, tts_dir, speedup)
    return dialogue
