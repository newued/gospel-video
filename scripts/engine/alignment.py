# -*- coding: utf-8 -*-
"""对齐引擎（v5 架构）—— 把音频对齐到每条消息的时间点。

三种模式：
  - VAD：能量曲线双阈值状态机 + 简单顺序匹配 + 静音检测兜底
        （原 audio_aligner.py 的检测逻辑迁入本文件；audio_aligner 保留为兼容转发）
  - ASR：预留（需 Whisper），未实现时 raise NotImplementedError
  - MANUAL：读取人工标注的时间点 JSON
  - AUTO：有 timings_path 走 MANUAL；有 audio_path 走 VAD 整曲对齐；
          否则用文本时长估算（_estimate_text_duration）

输出契约：
  - 回填每条 Message.audio.start_s / end_s
  - 整曲模式下把 song_audio / total_duration_s / alignment_method 写入 dialogue.meta
"""
from __future__ import annotations

import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import wave
from typing import Dict, List, Optional, Tuple

from .models import AudioClip, Dialogue, Message

# 旁白角色名集合（与 gospel_automator 判断口径一致）
_NARRATOR_ROLES = ("旁白", "narrator", "解说", "画外音")


# ---------------------------------------------------------------------------
# 消息访问辅助（兼容 engine.Message 与旧 dict 两种形态）
# ---------------------------------------------------------------------------

def _msg_text(msg) -> str:
    """取消息文本（兼容 dict 与 Message）。"""
    return msg.get("text", "") if isinstance(msg, dict) else (msg.text or "")


def _msg_role(msg) -> str:
    """取消息角色名。"""
    return msg.get("role", "") if isinstance(msg, dict) else (msg.role or "")


def _msg_is_narration(msg) -> bool:
    """判断是否旁白消息。"""
    if isinstance(msg, dict):
        return bool(msg.get("is_narration")) or msg.get("role") in _NARRATOR_ROLES
    return bool(msg.narration) or msg.role in _NARRATOR_ROLES


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe 工具
# ---------------------------------------------------------------------------

def _find_ffmpeg() -> Optional[str]:
    """查找可用的 ffmpeg，优先 imageio-ffmpeg（与 audio_aligner 一致）。"""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        pass
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        return ffmpeg
    return None


def _find_ffprobe() -> Optional[str]:
    """查找可用的 ffprobe（优先 imageio-ffmpeg 同目录）。"""
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
        if os.path.isfile(ffprobe):
            return ffprobe
    except ImportError:
        pass
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        return ffprobe
    return None


def get_audio_duration(audio_path: str) -> float:
    """获取音频时长（秒）。多层 fallback（与 audio_aligner 一致）。"""
    # 1. ffprobe
    ffprobe = _find_ffprobe()
    if ffprobe:
        try:
            cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", audio_path]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and r.stdout.strip():
                return float(r.stdout.strip())
        except Exception:
            pass

    # 2. ffmpeg stderr
    ffmpeg = _find_ffmpeg()
    if ffmpeg:
        try:
            cmd = [ffmpeg, "-i", audio_path, "-f", "null", "-"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", r.stderr)
            if m:
                h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
                return h * 3600 + mi * 60 + s
        except Exception:
            pass

    # 3. wave module（WAV）
    try:
        if audio_path.lower().endswith(".wav"):
            with wave.open(audio_path, "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                return frames / float(rate)
    except Exception:
        pass

    return 30.0  # 默认30秒


# ---------------------------------------------------------------------------
# 能量曲线提取 / VAD 语音段检测 / 静音检测
# ---------------------------------------------------------------------------

def extract_energy_curve(audio_path: str, frame_ms: int = 20) -> Tuple[List[float], float]:
    """用ffmpeg提取音频RMS能量曲线。返回 (energies, frame_duration_ms)。"""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg不可用")

    # 转为单声道16bit PCM，通过astats获取RMS
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        cmd = [
            ffmpeg, "-y", "-i", audio_path,
            "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
            tmp.name
        ]
        subprocess.run(cmd, capture_output=True, timeout=120)

        with wave.open(tmp.name, "rb") as wf:
            nframes = wf.getnframes()
            framerate = wf.getframerate()
            raw = wf.readframes(nframes)

        samples = struct.unpack(f"<{len(raw)//2}h", raw)
        samples = [s / 32768.0 for s in samples]

        frame_size = int(framerate * frame_ms / 1000)
        energies = []
        for i in range(0, len(samples) - frame_size, frame_size):
            frame = samples[i:i + frame_size]
            rms = math.sqrt(sum(s * s for s in frame) / len(frame))
            energies.append(rms)

        return energies, float(frame_ms)
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def detect_voice_segments(energies: List[float], frame_ms: int = 20,
                          high_ratio: float = 0.4, low_ratio: float = 0.15,
                          min_voice_ms: int = 300, min_silence_ms: int = 500,
                          pad_ms: int = 80) -> List[Tuple[float, float]]:
    """基础VAD：双阈值状态机。不做复杂的段内切分。"""
    if not energies:
        return []

    sorted_e = sorted(energies)
    p90 = sorted_e[int(len(sorted_e) * 0.90)]
    p20 = sorted_e[int(len(sorted_e) * 0.20)]
    max_e = max(energies)

    high_thresh = p90 * high_ratio
    low_thresh = p20 + (max_e - p20) * low_ratio * 0.5
    if low_thresh >= high_thresh:
        low_thresh = high_thresh * 0.5

    # 状态机
    states = [0] * len(energies)
    state = 0
    for i, e in enumerate(energies):
        if state == 0 and e >= high_thresh:
            state = 1
        elif state == 1 and e < low_thresh:
            state = 0
        states[i] = state

    segments_f = []
    in_voice = False
    start_f = 0
    for i, s in enumerate(states):
        if s == 1 and not in_voice:
            start_f = i
            in_voice = True
        elif s == 0 and in_voice:
            segments_f.append((start_f, i))
            in_voice = False
    if in_voice:
        segments_f.append((start_f, len(states)))

    # 过滤过短语音段
    min_voice_frames = min_voice_ms // frame_ms
    segments_f = [(s, e) for s, e in segments_f if (e - s) >= min_voice_frames]

    # 合并近邻短间隙
    min_silence_frames = min_silence_ms // frame_ms
    merged = []
    for seg in segments_f:
        if merged and seg[0] - merged[-1][1] < min_silence_frames:
            merged[-1] = (merged[-1][0], seg[1])
        else:
            merged.append(seg)

    # 转秒，加padding
    pad_frames = pad_ms // frame_ms
    result = []
    for s, e in merged:
        s_pad = max(0, s - pad_frames)
        e_pad = min(len(energies), e + pad_frames)
        start_sec = s_pad * frame_ms / 1000.0
        end_sec = e_pad * frame_ms / 1000.0
        result.append((start_sec, end_sec))

    return result


def detect_silences(audio_path: str, noise_db: float = -25.0,
                    min_silence_dur: float = 0.35) -> List[Tuple[float, float]]:
    """ffmpeg silencedetect 检测静音区间 [(start, end)]。"""
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        return []
    cmd = [
        ffmpeg, "-hide_banner", "-nostats", "-i", audio_path,
        "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_dur}",
        "-f", "null", "-"
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    output = r.stderr
    starts = [float(x) for x in re.findall(r"silence_start:\s*([\d.]+)", output)]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([\d.]+)", output)]
    return list(zip(starts, ends))


# ---------------------------------------------------------------------------
# 文本时长估算
# ---------------------------------------------------------------------------

def _estimate_text_duration(text: str) -> float:
    """按字符构成估算一句话的朗读时长（秒）：cn*0.35 + en*0.25 + punct*0.15 + 0.3。"""
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    en_words = len(re.findall(r"[a-zA-Z]+", text))
    punct = len(re.findall(r"[。！？!?…，,]", text))
    return cn * 0.35 + en_words * 0.25 + punct * 0.15 + 0.3


# ---------------------------------------------------------------------------
# 简单顺序匹配 / 静音兜底匹配
# ---------------------------------------------------------------------------

def _simple_align(messages: list, voice_segments: List[Tuple[float, float]],
                  total_duration: float) -> List[Tuple[float, float]]:
    """最简单的顺序1:1分配：第i句对应第i个语音段。

    - 句数 <= 段数：前n-1句各拿1段，最后一句拿剩余所有段
    - 句数 > 段数：前m句各拿1段，剩余句子按文本权重分配剩余时间
    """
    n = len(messages)
    m = len(voice_segments)

    if m == 0:
        weights = [_estimate_text_duration(_msg_text(msg)) for msg in messages]
        total_w = sum(weights) or 1.0
        result = []
        t = 0.2
        for w in weights:
            dur = (total_duration - 0.4) * w / total_w
            result.append((round(t, 2), round(t + dur, 2)))
            t += dur
        return result

    result = []
    take = min(n - 1, m)  # 前n-1句各拿1段
    for i in range(take):
        s, e = voice_segments[i]
        result.append((round(s, 2), round(e, 2)))

    if n <= m:
        # 最后一句拿剩余所有段
        s = voice_segments[take][0]
        e = voice_segments[-1][1]
        result.append((round(s, 2), round(e, 2)))
    else:
        # 句子比段多，剩余句子按权重分配最后一段结束到总时长
        last_seg_end = voice_segments[-1][1]
        remaining = n - take
        weights_left = [_estimate_text_duration(_msg_text(messages[j])) for j in range(take, n)]
        total_w_left = sum(weights_left) or 1.0
        t = last_seg_end
        available = total_duration - t - 0.1  # 剩余可用时间
        for i, w in enumerate(weights_left):
            if i == len(weights_left) - 1:
                # 最后一句：拿所有剩余时间
                dur = available
            else:
                dur = max(0.3, available * w / total_w_left)
            # 确保不超出总时长
            dur = min(dur, total_duration - t - 0.05)
            dur = max(dur, 0.05)  # 至少 0.05s，避免 0 或负时长
            result.append((round(t, 2), round(t + dur, 2)))
            t += dur

    return result


def _fallback_align(messages: list, total_duration: float,
                    silences: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """基于静音检测的fallback对齐：在静音中点切分。"""
    n = len(messages)
    weights = [_estimate_text_duration(_msg_text(msg)) for msg in messages]
    total_w = sum(weights) or 1.0

    if not silences or n <= 1:
        result = []
        t = 0.3
        for w in weights:
            dur = (total_duration - 0.6) * w / total_w
            result.append((round(t, 2), round(t + dur, 2)))
            t += dur
        return result

    # 期望切分点
    expected = []
    cum = 0
    for i in range(n - 1):
        cum += weights[i]
        expected.append(total_duration * cum / total_w)

    # 用最近的静音中点匹配
    boundaries = []
    used = set()
    valid_silences = [(s, e) for s, e in silences if s > 0.5 and e < total_duration - 0.5]
    for exp in expected:
        best = None
        best_dist = float("inf")
        for si, (s, e) in enumerate(valid_silences):
            if si in used:
                continue
            mid = (s + e) / 2
            dist = abs(mid - exp)
            if dist < best_dist:
                best_dist = dist
                best = (si, mid)
        if best is not None and best_dist < 3.0:
            used.add(best[0])
            boundaries.append(best[1])
        else:
            boundaries.append(exp)
    boundaries.sort()

    result = []
    prev_end = 0.3
    for i in range(n):
        end = boundaries[i] if i < len(boundaries) else total_duration - 0.3
        start = prev_end
        if end - start < 0.4:
            end = min(start + 0.5, total_duration - 0.1)
        result.append((round(start, 2), round(end, 2)))
        prev_end = end
    return result


# ---------------------------------------------------------------------------
# 整曲对齐核心（VAD 主流程）
# ---------------------------------------------------------------------------

def _align_song_to_messages(full_audio_path: str, messages: list,
                            noise_db: float = -25.0,
                            min_silence_dur: float = 0.35) -> dict:
    """对齐整首歌曲到消息列表。旁白不在音频中时占位插入并标记 manual。

    返回对齐结果 dict（结构与 audio_aligner.align_song_to_messages 一致）。
    """
    total_dur = get_audio_duration(full_audio_path)

    # 识别旁白
    narration_indices = []
    for i, msg in enumerate(messages):
        if _msg_is_narration(msg):
            narration_indices.append(i)

    n_total = len(messages)
    n_song = n_total - len(narration_indices)

    # VAD 检测
    voice_segments = []
    used_method = "vad"
    try:
        energies, _ = extract_energy_curve(full_audio_path)
        voice_segments = detect_voice_segments(energies)
        print(f"   VAD检测到 {len(voice_segments)} 个语音段")
    except Exception as e:
        print(f"   VAD失败: {e}，回退到静音检测")
        used_method = "silence"

    # 判断旁白是否在音频中
    narrations_in_audio = False
    if voice_segments and narration_indices:
        ratio_all = len(voice_segments) / n_total if n_total > 0 else 0
        ratio_song = len(voice_segments) / n_song if n_song > 0 else 0
        dist_all = abs(ratio_all - 1)
        dist_song = abs(ratio_song - 1)
        if dist_all < dist_song and 0.75 <= ratio_all <= 1.35:
            narrations_in_audio = True

    # 确定参与匹配的消息
    if narrations_in_audio:
        msgs_to_match = list(messages)
        song_indices = list(range(n_total))
    else:
        msgs_to_match = [m for i, m in enumerate(messages) if i not in narration_indices]
        song_indices = [i for i in range(n_total) if i not in narration_indices]

    n_match = len(msgs_to_match)

    # 执行匹配
    if voice_segments and used_method == "vad":
        ratio = len(voice_segments) / n_match if n_match > 0 else 0
        if ratio < 0.4 or ratio > 3:
            silences = detect_silences(full_audio_path, noise_db, min_silence_dur)
            timings = _fallback_align(msgs_to_match, total_dur, silences)
            used_method = "silence_fallback"
        else:
            timings = _simple_align(msgs_to_match, voice_segments, total_dur)
    else:
        silences = detect_silences(full_audio_path, noise_db, min_silence_dur)
        timings = _fallback_align(msgs_to_match, total_dur, silences)
        used_method = "silence_fallback"

    # 构建结果
    alignment = [None] * n_total
    for match_i, orig_i in enumerate(song_indices):
        s, e = timings[match_i]
        is_narr = orig_i in narration_indices
        alignment[orig_i] = {
            "index": orig_i,
            "role": _msg_role(messages[orig_i]),
            "text": _msg_text(messages[orig_i]),
            "is_narration": is_narr,
            "start_s": s,
            "end_s": e,
            "in_song": True,
            "manual": is_narr and not narrations_in_audio,
        }

    # 旁白占位（不在音频中的旁白，插到相邻歌词间隙中间，2s，manual=True）
    for n_idx in narration_indices:
        if alignment[n_idx] is not None:
            alignment[n_idx]["manual"] = False
            continue
        # 找前后歌词位置
        prev_song = None
        next_song = None
        for si in song_indices:
            if si < n_idx:
                prev_song = si
            if si > n_idx and next_song is None:
                next_song = si
                break
        if prev_song is not None and next_song is not None:
            gap_s = alignment[prev_song]["end_s"]
            gap_e = alignment[next_song]["start_s"]
        elif prev_song is not None:
            gap_s = alignment[prev_song]["end_s"]
            gap_e = total_dur
        elif next_song is not None:
            gap_s = 0
            gap_e = alignment[next_song]["start_s"]
        else:
            gap_s, gap_e = 0, total_dur

        mid = (gap_s + gap_e) / 2
        # 时长适配间隙：间隙不足 2s 时按间隙 80% 收缩，避免负时长
        dur = min(2.0, (gap_e - gap_s) * 0.8)
        ns = max(gap_s + 0.1, mid - dur / 2)
        ne = min(gap_e - 0.1, ns + dur)
        # 不越出歌曲总时长，且保证正时长
        ne = min(ne, total_dur)
        if ne <= ns:
            ne = min(ns + 0.1, total_dur)
            if ne > total_dur:
                ns = max(total_dur - 0.1, gap_s)
                ne = total_dur
        if ne <= ns:
            ns, ne = total_dur, total_dur
        alignment[n_idx] = {
            "index": n_idx,
            "role": _msg_role(messages[n_idx]) or "旁白",
            "text": _msg_text(messages[n_idx]),
            "is_narration": True,
            "start_s": round(ns, 2),
            "end_s": round(ne, 2),
            "in_song": False,
            "manual": True,
        }

    return {
        "full_audio": os.path.abspath(full_audio_path),
        "total_duration": round(total_dur, 2),
        "song_message_count": n_song,
        "narration_count": len(narration_indices),
        "narrations_in_audio": narrations_in_audio,
        "alignment_method": used_method,
        "voice_segments": [(round(s, 2), round(e, 2)) for s, e in voice_segments],
        "alignment": [a for a in alignment if a is not None],
    }


# ---------------------------------------------------------------------------
# 整曲音频文件查找
# ---------------------------------------------------------------------------

_FULL_SONG_NAMES = (
    "full.mp3", "full.wav", "full.m4a", "full.aac", "full.ogg",
    "song.mp3", "song.wav", "song.m4a",
    "full_song.mp3", "full_song.wav",
    "complete.mp3", "complete.wav",
    "整曲.mp3", "整曲.wav", "歌曲.mp3", "歌曲.wav",
)


def find_full_song_audio(directory: str) -> Optional[str]:
    """在指定目录查找整曲音频文件（Suno/妙响生成的完整歌曲）。

    查找优先级：
    1. 精确匹配标准文件名（full.mp3, song.mp3等）
    2. 文件名包含full/song/整曲/歌曲且不是序号格式（000.mp3, voice_000.mp3等）
    """
    if not os.path.isdir(directory):
        return None

    # 1. 精确匹配
    for name in _FULL_SONG_NAMES:
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            return os.path.abspath(path)

    # 2. 模糊匹配（排除序号文件）
    exts = (".mp3", ".wav", ".m4a", ".aac", ".ogg")
    for fname in os.listdir(directory):
        lower = fname.lower()
        if not any(lower.endswith(ext) for ext in exts):
            continue
        # 排除序号文件：000.mp3, voice_000.mp3, a0.mp3 等
        if re.match(r"^[a-z]*[_-]?\d+\.", lower):
            continue
        # 包含关键词
        if any(kw in lower for kw in ("full", "song", "整曲", "歌曲", "complete", "whole")):
            return os.path.abspath(os.path.join(directory, fname))

    return None


# ---------------------------------------------------------------------------
# timings 导入 / 导出
# ---------------------------------------------------------------------------

def export_timings(dialogue: Dialogue, out_path: str):
    """把 dialogue 的当前对齐结果导出为 timings JSON（供人工微调）。"""
    messages = []
    for i, msg in enumerate(dialogue.messages):
        messages.append({
            "index": i,
            "role": _msg_role(msg),
            "text": _msg_text(msg),
            "is_narration": _msg_is_narration(msg),
            "start_s": round(msg.audio.start_s, 2),
            "end_s": round(msg.audio.end_s, 2),
            "manual": bool(msg.audio.manual),
        })
    data = {
        "full_audio": dialogue.meta.get("song_audio", ""),
        "total_duration": dialogue.meta.get("total_duration_s", 0.0),
        "messages": messages,
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_timings(timings_path: str) -> dict:
    """读取 timings JSON，返回规范化的对齐结果 dict。

    支持两种输入：
      {messages: [{index, start_s, end_s, manual}]}                     （精简格式）
      {full_audio, total_duration, messages: [{index, role, text,
        is_narration, start_s, end_s, manual}]}                        （导出格式）
    """
    with open(timings_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    messages = []
    for m in data.get("messages", []):
        messages.append({
            "index": m["index"],
            "role": m.get("role", ""),
            "text": m.get("text", ""),
            "is_narration": m.get("is_narration", False),
            "start_s": m["start_s"],
            "end_s": m["end_s"],
            "in_song": not m.get("is_narration", False) or not m.get("manual", False),
            "manual": m.get("manual", False),
        })
    return {
        "full_audio": data.get("full_audio", ""),
        "total_duration": data.get("total_duration", 0.0),
        "song_message_count": sum(1 for a in messages if not a["is_narration"]),
        "narration_count": sum(1 for a in messages if a["is_narration"]),
        "narrations_in_audio": any(a["is_narration"] and not a["manual"] for a in messages),
        "alignment_method": "manual",
        "voice_segments": [],
        "alignment": messages,
    }


# ---------------------------------------------------------------------------
# ASR 逐句对齐（faster-whisper，毫秒级精度）
# ---------------------------------------------------------------------------
# 实现思路下沉自 ab_generator.py 的 run_asr / asr_align，使默认流水线
# 也能享受"精确到毫秒的音轨分析"（文章2 的核心诉求），不再依赖 VAD 静音检测。

_ASR_MODEL = "small"
_ASR_DEVICE = "cpu"
_ASR_COMPUTE = "int8"


def _normalize_text(s: str) -> str:
    """归一化中文文本（去标点/空白，便于相似度比对）。"""
    import re
    return re.sub(r"[\s，。！？、,.!?；;：:…—（）()\"'“”‘’]", "", s or "")


def run_asr(audio_path: str, model: str = _ASR_MODEL,
            device: str = _ASR_DEVICE, compute: str = _ASR_COMPUTE) -> List[dict]:
    """faster-whisper 转写音频，返回 [{start, end, text}]（秒，毫秒精度）。

    依赖 faster-whisper；未安装时抛 RuntimeError，由上层降级到 VAD。
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError(f"faster-whisper 未安装，无法 ASR 对齐：{e}")

    try:
        model_obj = WhisperModel(model, device=device, compute_type=compute)
    except Exception as e:
        raise RuntimeError(
            f"ASR 模型加载失败（{model}/{device}/{compute}）：{e}")

    segs: List[dict] = []
    seg_iter, _info = model_obj.transcribe(audio_path, language="zh")
    for s in seg_iter:
        text = (s.text or "").strip()
        if text:
            segs.append({
                "start": round(float(s.start), 3),
                "end": round(float(s.end), 3),
                "text": text,
            })
    return segs


def asr_align(dialogs: List, asr_segs: List[dict], total_duration: float) -> List[dict]:
    """把 ASR 分段时间戳贪心对齐到对话文本。

    dialogs: [(role, content)] 或 [Message]；asr_segs: [{start,end,text}] 按时间序。
    返回 [{role, text, audio_start, audio_end}]（毫秒精度）。
    策略：从当前 ASR 指针向后最多合并 3 段，取与对话文本相似度最高窗口；
          若某句完全无匹配，用前后句时间区间插值。
    """
    import difflib

    def _text_of(d) -> str:
        return d.text if hasattr(d, "text") else (d.get("text") if isinstance(d, dict) else str(d))

    def _role_of(d) -> str:
        return d.role if hasattr(d, "role") else (d.get("role", "") if isinstance(d, dict) else "")

    contents = [(_role_of(d), _text_of(d)) for d in dialogs]

    # 兜底：无任何转写结果 → 按文本长度均匀分配
    if not asr_segs:
        total_chars = sum(len(c) for _, c in contents) or 1
        cursor = 0.0
        out = []
        for role, content in contents:
            dur = max(0.3, len(content) / total_chars * total_duration)
            out.append({
                "role": role, "text": content,
                "audio_start": round(cursor, 3),
                "audio_end": round(min(cursor + dur, total_duration), 3),
            })
            cursor = min(cursor + dur, total_duration)
        return out

    n = len(asr_segs)
    i_a = 0
    prev_end = 0.0
    results = []
    for role, content in contents:
        nd = _normalize_text(content)
        best_ratio, best_b = -1.0, None
        for k in range(1, 4):
            if i_a + k > n:
                break
            merged = "".join(_normalize_text(s["text"]) for s in asr_segs[i_a:i_a + k])
            if not merged:
                continue
            r = difflib.SequenceMatcher(None, nd, merged).ratio()
            if r > best_ratio:
                best_ratio, best_b = r, i_a + k

        if best_b is not None and best_ratio >= 0.25:
            audio_start = asr_segs[i_a]["start"]
            audio_end = asr_segs[best_b - 1]["end"]
            i_a = best_b
        else:
            audio_start = prev_end
            est = max(0.5, min(len(content) * 0.25, 8.0))
            if i_a < n:
                audio_end = min(asr_segs[i_a]["start"], total_duration)
                if audio_end - audio_start < 0.3:
                    audio_end = min(audio_start + est, total_duration)
            else:
                audio_end = min(audio_start + est, total_duration)
            audio_end = max(audio_end, audio_start + 0.2)

        audio_start = max(0.0, min(audio_start, total_duration))
        audio_end = max(audio_start + 0.1, min(audio_end, total_duration))
        results.append({
            "role": role, "text": content,
            "audio_start": round(audio_start, 3),
            "audio_end": round(audio_end, 3),
        })
        prev_end = max(prev_end, audio_end)
    return results


def _asr_align_dialogue(dialogue: Dialogue, audio_path: str) -> Dialogue:
    """用 ASR 对整段音频逐句对齐，回填每条 Message.audio（毫秒级精度）。"""
    total = get_audio_duration(audio_path)
    segs = run_asr(audio_path)
    aligned = asr_align(dialogue.messages, segs, total)

    # 识别旁白
    narr_indices = {i for i, m in enumerate(dialogue.messages) if m.narration}

    for i, a in enumerate(aligned):
        msg = dialogue.messages[i]
        msg.audio.start_s = a["audio_start"]
        msg.audio.end_s = a["audio_end"]
        msg.audio.source = "song" if i not in narr_indices else "none"
        msg.audio.manual = (i in narr_indices)  # 旁白不在歌词中，标记 manual 占位

    dialogue.meta["song_audio"] = os.path.abspath(audio_path)
    dialogue.meta["total_duration_s"] = round(total, 3)
    dialogue.meta["alignment_method"] = "asr"
    dialogue.meta["narrations_in_audio"] = False
    return dialogue


# ---------------------------------------------------------------------------
# AlignmentEngine（门面）
# ---------------------------------------------------------------------------

class AlignmentEngine:
    """对齐引擎：ASR / VAD / MANUAL / AUTO 四种模式（v6 升级）。

    对外契约：
      align(dialogue, audio_path=None, timings_path=None) -> Dialogue
        ASR ：faster-whisper 逐句转写对齐（毫秒级精度，需 faster-whisper）
        VAD ：能量曲线/静音检测整曲对齐（零额外依赖）
        MANUAL：读取人工标注的时间点 JSON
        AUTO：timings_path > audio_path(优先 ASR，失败降级 VAD 整曲) > 文本估算
      align_song(dialogue, full_audio_path) -> Dialogue   整曲对齐（ASR 优先）
      load_manual(dialogue, timings_path) -> Dialogue     手工标注时间
    """

    def __init__(self, mode: str = "AUTO", noise_db: float = -25,
                 min_silence_dur: float = 0.35, silence_db: float = -25,
                 prefer_asr: bool = True) -> None:
        self.mode = (mode or "AUTO").upper()
        self.noise_db = noise_db
        self.min_silence_dur = min_silence_dur
        self.silence_db = silence_db
        # v6：AUTO / VAD 在有音频时是否优先尝试 ASR（失败安全降级 VAD）
        self.prefer_asr = prefer_asr

    def align(self, dialogue: Dialogue, audio_path: Optional[str] = None,
              timings_path: Optional[str] = None) -> Dialogue:
        """按模式对齐，回填每条 Message.audio.start_s / end_s。"""
        mode = self.mode

        if mode == "MANUAL":
            if not timings_path:
                raise ValueError("MANUAL 模式需要 timings_path")
            return self.load_manual(dialogue, timings_path)

        if mode == "ASR":
            if not audio_path:
                raise ValueError("ASR 模式需要 audio_path（整曲音频）")
            return self._asr_align(dialogue, audio_path)

        if mode == "VAD":
            if not audio_path:
                raise ValueError("VAD 模式需要 audio_path（整曲音频）")
            return self.align_song(dialogue, audio_path)

        # AUTO：优先 MANUAL -> 有音频时优先 ASR（失败降级 VAD）-> 文本估算
        if timings_path:
            return self.load_manual(dialogue, timings_path)
        if audio_path:
            if self.prefer_asr:
                try:
                    return self._asr_align(dialogue, audio_path)
                except Exception as e:
                    print(f"   ASR 对齐失败，降级到 VAD：{e}")
            return self.align_song(dialogue, audio_path)
        return self._estimate_align(dialogue)

    def _asr_align(self, dialogue: Dialogue, audio_path: str) -> Dialogue:
        """ASR 逐句对齐（毫秒级），封装自 ab_generator 的 faster-whisper 实现。"""
        return _asr_align_dialogue(dialogue, audio_path)

    def align_song(self, dialogue: Dialogue, full_audio_path: str) -> Dialogue:
        """整曲对齐：ASR 优先（毫秒级），失败回退 VAD 整曲对齐。

        回填 audio 并把整曲信息写入 meta。
        """
        if self.prefer_asr:
            try:
                return self._asr_align(dialogue, full_audio_path)
            except Exception as e:
                print(f"   ASR 整曲对齐失败，回退 VAD：{e}")
        result = _align_song_to_messages(
            full_audio_path, dialogue.messages,
            noise_db=self.noise_db, min_silence_dur=self.min_silence_dur,
        )
        by_index = {a["index"]: a for a in result["alignment"]}
        for i, msg in enumerate(dialogue.messages):
            a = by_index.get(i)
            if a is None:
                continue
            msg.audio.start_s = a["start_s"]
            msg.audio.end_s = a["end_s"]
            msg.audio.manual = bool(a["manual"])
            msg.audio.source = "song"
        dialogue.meta["song_audio"] = result["full_audio"]
        dialogue.meta["total_duration_s"] = result["total_duration"]
        dialogue.meta["alignment_method"] = result["alignment_method"]
        dialogue.meta["narrations_in_audio"] = result["narrations_in_audio"]
        return dialogue

    def load_manual(self, dialogue: Dialogue, timings_path: str) -> Dialogue:
        """加载手工标注的时间点，回填 audio.start_s / end_s 与 manual 标记。"""
        data = load_timings(timings_path)
        by_index = {a["index"]: a for a in data["alignment"]}
        for i, msg in enumerate(dialogue.messages):
            a = by_index.get(i)
            if a is None:
                continue
            msg.audio.start_s = a["start_s"]
            msg.audio.end_s = a["end_s"]
            msg.audio.manual = bool(a["manual"])
        dialogue.meta["total_duration_s"] = data["total_duration"]
        dialogue.meta["alignment_method"] = "manual"
        dialogue.meta["narrations_in_audio"] = data["narrations_in_audio"]
        return dialogue

    def _estimate_align(self, dialogue: Dialogue) -> Dialogue:
        """文本时长估算对齐：无音频/无手工时间点时按朗读时长顺序排布。"""
        msgs = dialogue.messages
        timings = []
        cursor = 0.5  # 首条偏移
        prev_role = None
        for msg in msgs:
            dur = max(0.4, _estimate_text_duration(_msg_text(msg)))
            if timings:
                cursor += 0.2 if (prev_role == msg.role) else 0.4
            timings.append((cursor, cursor + dur))
            cursor += dur
            prev_role = msg.role
        for msg, (s, e) in zip(msgs, timings):
            msg.audio.start_s = round(s, 2)
            msg.audio.end_s = round(e, 2)
            msg.audio.source = "none"
        dialogue.meta["total_duration_s"] = round(
            timings[-1][1] + 1.5, 2) if timings else 0.0
        dialogue.meta["alignment_method"] = "estimate"
        return dialogue
