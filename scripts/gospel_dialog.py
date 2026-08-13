# -*- coding: utf-8 -*-
"""福音对话视频一键生成工具（gospel_dialog）
================================================

直接粘贴对话文本（支持 [A][B][旁白] 标记），自动完成：
  1. 解析对话，识别角色、旁白、情绪、音色
  2. 三种音频模式：
     - auto:   自动TTS多角色配音（默认）
     - suno:   生成Suno/妙响提示词，暂停等待你生成音频
     - manual: 读取 assets/custom_audio/<project>/ 下手动放置的音频
  3. 音频驱动时序计算
  4. 微信聊天界面逐条弹出渲染（旁白特殊居中半透明样式）
  5. 智能贴纸（下半屏居中）/克制音效/随机转场
  6. 输出剪映草稿

说明：本入口 CLI 与旧版完全一致，内部逻辑走 v5 架构的 engine 流水线：
  - 解析/角色/情绪：engine.parser / engine.role_mapper / engine.emotion
    （经 dialog_parser 兼容转发层，保持旧 script 格式）
  - 手动整曲对齐：engine.alignment.AlignmentEngine（VAD 对齐 / load_timings 复用）
  - 自动 TTS + 时序：gospel_automator.build_gospel_video（内部走新流水线）

用法：
  # 最简单：从文本文件读取，自动TTS
  python gospel_dialog.py -f dialog.txt

  # 直接粘贴文本（用 \\n 换行）
  python gospel_dialog.py -T "[A]你好\\n[B]你好啊\\n[旁白]然后..."

  # 生成Suno提示词（手动生成音频后再执行manual模式）
  python gospel_dialog.py -f dialog.txt -m suno
  # -> 去Suno生成音频，下载为 000.mp3, 001.mp3... 放入提示的目录
  python gospel_dialog.py -f dialog.txt -m manual

  # 指定Suno曲风（gospel_funk/ballad/rap/pop）
  python gospel_dialog.py -f dialog.txt -m suno --suno-style rap

  # 只生成plan预览，不组装草稿
  python gospel_dialog.py -f dialog.txt --plan-only
"""
import argparse
import json
import os
import sys
import glob
import shutil

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, SCRIPT_DIR)

from dialog_parser import (  # noqa: E402
    parse_dialog_text, parse_dialog_file,
    generate_suno_prompt,
    NARRATOR_ROLES
)
from engine.alignment import (  # noqa: E402
    AlignmentEngine, find_full_song_audio,
    export_timings, load_timings,
)
from engine.models import Dialogue  # noqa: E402


def ensure_dir(p):
    os.makedirs(p, exist_ok=True)
    return p


def _external_alignment_from_timings(full_song: str, t_data: dict) -> dict:
    """把 load_timings 的规范化结果打包成 build_gospel_video 的 external_alignment 契约。

    旧代码直接取 t_data["messages"]（会 KeyError），这里统一用 "alignment" 键，
    并补齐展示用的统计字段。
    """
    return {
        "full_audio": full_song,
        "total_duration": t_data.get("total_duration", 0.0),
        "song_message_count": t_data.get("song_message_count", 0),
        "narration_count": t_data.get("narration_count", 0),
        "narrations_in_audio": t_data.get("narrations_in_audio", False),
        "alignment": t_data.get("alignment", t_data.get("messages", [])),
    }


def _dialogue_to_external_alignment(dialogue: Dialogue) -> dict:
    """把整曲对齐后的 Dialogue 转成 build_gospel_video 的 external_alignment 契约。

    build_plan 需要：full_audio / total_duration / narrations_in_audio /
    alignment（每条含 index/role/text/is_narration/start_s/end_s/manual）。
    """
    alignment = []
    for i, msg in enumerate(dialogue.messages):
        alignment.append({
            "index": i,
            "role": msg.role,
            "text": msg.text,
            "is_narration": bool(msg.narration),
            "start_s": msg.audio.start_s,
            "end_s": msg.audio.end_s,
            "in_song": msg.audio.source == "song",
            "manual": bool(msg.audio.manual),
        })
    return {
        "full_audio": dialogue.meta.get("song_audio", ""),
        "total_duration": dialogue.meta.get("total_duration_s", 0.0),
        "song_message_count": sum(1 for a in alignment if not a["is_narration"]),
        "narration_count": sum(1 for a in alignment if a["is_narration"]),
        "narrations_in_audio": dialogue.meta.get("narrations_in_audio", False),
        "alignment": alignment,
    }


def load_manual_audio(custom_dir: str, messages: list) -> dict:
    """扫描手动音频目录，返回 {index: source_path}。
    命名支持：000.mp3 / voice_000.mp3 / a0.mp3 等，按第一个数字匹配消息序号。
    """
    import re
    mapping = {}
    if not os.path.isdir(custom_dir):
        return mapping
    exts = ("*.mp3", "*.wav", "*.ogg", "*.m4a", "*.aac")
    files = []
    for ext in exts:
        files.extend(glob.glob(os.path.join(custom_dir, ext)))
    for fpath in sorted(files):
        fname = os.path.basename(fpath)
        nums = re.findall(r"(\d+)", fname)
        if nums:
            idx = int(nums[0])
            if 0 <= idx < len(messages):
                mapping[idx] = fpath
    return mapping


def patch_tts_with_manual(manual_map: dict, tts_dir: str, messages: list):
    """把手动音频复制到TTS目录，返回一个补丁函数来替换 _tts_segment。"""
    import gospel_automator as ga
    original_tts = ga._tts_segment
    original_speedup = ga._speedup_audio

    # 预复制手动音频到 tts 目录
    for idx, src in manual_map.items():
        dst = os.path.join(tts_dir, f"voice_{idx:03d}.mp3")
        ext = os.path.splitext(src)[1].lower()
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        if ext == ".mp3":
            shutil.copy2(src, dst)
        else:
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-i", src, "-acodec", "libmp3lame", "-q:a", "2", dst],
                capture_output=True, timeout=30
            )

    def _patched_tts(text, speaker, index):
        if index in manual_map:
            out = os.path.join(tts_dir, f"voice_{index:03d}.mp3")
            if os.path.exists(out):
                role = messages[index]["role"] if index < len(messages) else "?"
                print(f"  [消息{index:02d}] 使用手动音频: {os.path.basename(manual_map[index])} ({role})")
                return out
        return original_tts(text, speaker, index)

    def _no_speedup(path, speed, index):
        return path  # 手动音频不做变速

    ga._tts_segment = _patched_tts
    ga._speedup_audio = _no_speedup
    return original_tts, original_speedup


def main():
    parser = argparse.ArgumentParser(
        description="福音对话视频一键生成 — 粘贴对话直接出剪映草稿",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--text", "-T", type=str, help="直接传入对话文本（\\n 换行）")
    src.add_argument("--file", "-f", type=str, help="从文本文件读取对话")

    parser.add_argument("--title", "-t", type=str, default=None, help="视频标题")
    parser.add_argument("--project-name", "-p", type=str, default=None, help="项目名")
    parser.add_argument("--audio-mode", "-m", type=str, default="auto",
                        choices=("auto", "manual", "suno"),
                        help="音频模式: auto(默认TTS)/manual(手动音频)/suno(生成提示词)")
    parser.add_argument("--suno-style", type=str, default="gospel_funk",
                        choices=("gospel_funk", "ballad", "rap", "pop"),
                        help="Suno曲风")
    parser.add_argument("--bgm", type=str, default=None, help="BGM关键词")
    parser.add_argument("--speed", type=float, default=1.3, help="全局倍速")
    parser.add_argument("--plan-only", action="store_true", help="只生成plan，不组装草稿")
    parser.add_argument("--audio-dir", type=str, default=None, help="手动音频目录")
    parser.add_argument("--timings", type=str, default=None, help="用户微调过的时间点JSON文件（整曲模式）")
    parser.add_argument("--silence-db", type=float, default=-25.0, help="静音检测阈值dB（默认-25）")
    parser.add_argument("--draft-root", type=str, default=None, help="剪映草稿根目录")
    parser.add_argument("--export", type=str, default=None, help="导出视频路径（需剪映≤5.9）")
    args = parser.parse_args()

    # ---- 1. 解析对话 ----
    print("=" * 60)
    print("步骤1: 解析对话文本")
    print("=" * 60)
    if args.file:
        script = parse_dialog_file(args.file, title=args.title)
    else:
        text = args.text.replace("\\n", "\n")
        script = parse_dialog_text(text, title=args.title, project_name=args.project_name)

    if args.bgm:
        script["bgm_query"] = args.bgm
    if args.project_name:
        script["project_name"] = args.project_name

    project_name = script["project_name"]
    narrator_count = sum(1 for m in script["messages"] if m.get("is_narration"))
    print(f"  标题: {script['title']}")
    print(f"  项目名: {project_name}")
    print(f"  BGM: {script['bgm_query']}")
    print(f"  消息数: {len(script['messages'])}")
    roles = {}
    for m in script["messages"]:
        roles[m["role"]] = roles.get(m["role"], 0) + 1
    for role, cnt in roles.items():
        tag = " [旁白]" if role in NARRATOR_ROLES else ""
        print(f"    - {role}: {cnt}条{tag}")
    print(f"  音色映射:")
    for role, sp in script.get("role_speakers", {}).items():
        print(f"    {role} -> {sp}")

    # ---- 2. 保存剧本到临时JSON ----
    ASSETS_DIR = ensure_dir(os.path.join(PROJECT_ROOT, "assets"))
    TTS_DIR = ensure_dir(os.path.join(ASSETS_DIR, "tts"))
    CUSTOM_DIR = args.audio_dir or ensure_dir(os.path.join(ASSETS_DIR, "custom_audio", project_name))
    script_json = os.path.join(ASSETS_DIR, f"_tmp_script_{project_name}.json")
    with open(script_json, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)

    # 清理旧TTS
    for old in glob.glob(os.path.join(TTS_DIR, "voice_*.mp3")):
        try:
            os.remove(old)
        except OSError:
            pass

    # ---- 3. Suno模式：生成提示词后暂停 ----
    if args.audio_mode == "suno":
        print("\n" + "=" * 60)
        print("步骤2: 生成Suno/妙响提示词")
        print("=" * 60)
        song_prompt = generate_suno_prompt(script, style=args.suno_style)
        prompt_file = os.path.join(ASSETS_DIR, f"song_prompt_{project_name}.txt")
        with open(prompt_file, "w", encoding="utf-8") as f:
            f.write(song_prompt)

        # 清理旧的重复文件
        for old_name in (f"suno_prompt_{project_name}.txt", f"miaoxiang_prompt_{project_name}.txt"):
            try:
                os.remove(os.path.join(ASSETS_DIR, old_name))
            except OSError:
                pass

        print(f"\nSuno/妙响提示词（已保存: {prompt_file}）：")
        print("-" * 60)
        print(song_prompt)
        print("-" * 60)
        print(f"\n手动音频目录: {CUSTOM_DIR}")
        os.makedirs(CUSTOM_DIR, exist_ok=True)
        print("=" * 60)
        print("下一步操作：")
        print("  1. 复制上面的提示词到 Suno 或 妙响 生成歌曲")
        print("  2. 下载生成的完整歌曲音频文件")
        print(f"  3. 命名为 full.mp3 放入以下目录：")
        print(f"     {CUSTOM_DIR}")
        print("     （支持的文件名：full.mp3, song.mp3, full_song.mp3, full.wav 等）")
        print("  4. 如果歌曲中没有旁白部分，系统会自动用TTS生成旁白叠加")
        print("  5. 执行以下命令继续（自动检测静音对齐画面）：")
        if args.file:
            print(f"     python gospel_dialog.py -f {args.file} -m manual")
        else:
            print(f"     python gospel_dialog.py -T \"你的文本\" -m manual")
        print()
        print("  高级：如果自动对齐不准确，可以：")
        print(f"     a) 首次运行后编辑 {CUSTOM_DIR} 目录下的 timings_*.json 调整时间点")
        print(f"     b) 重跑时加 --timings <文件> 加载微调后的时间点")
        print(f"     c) 用 --silence-db 调整静音检测灵敏度（默认-25dB，值越小越严格）")
        print("\n已暂停，等待音频准备好后再运行。")
        return

    # ---- 4. 手动音频模式：加载用户音频 ----
    originals = None
    external_alignment = None

    if args.audio_mode == "manual":
        print("\n" + "=" * 60)
        print("步骤2: 加载手动音频")
        print("=" * 60)
        print(f"  目录: {CUSTOM_DIR}")

        # 检测是否有整曲音频（Suno/妙响生成的完整歌曲）
        full_song = find_full_song_audio(CUSTOM_DIR)

        if full_song:
            # ---- 整曲模式：engine AlignmentEngine 自动对齐 ----
            print(f"\n检测到整曲音频: {os.path.basename(full_song)}")
            print("   正在分析音频时间轴（静音检测）...")

            timings_file = args.timings
            auto_timings_file = os.path.join(CUSTOM_DIR, f"timings_{project_name}.json")

            if timings_file and os.path.isfile(timings_file):
                # 用户提供了微调过的时间点
                print(f"   使用用户微调时间点: {timings_file}")
                t_data = load_timings(timings_file)
                external_alignment = _external_alignment_from_timings(full_song, t_data)
            elif os.path.isfile(auto_timings_file):
                # 已有自动检测的时间点（用户可能微调过）
                print(f"   复用已有时间点文件: {auto_timings_file}")
                print(f"   如需重新检测，请删除该文件后重跑")
                t_data = load_timings(auto_timings_file)
                external_alignment = _external_alignment_from_timings(full_song, t_data)
            else:
                # 自动检测：构造 Dialogue -> AlignmentEngine 整曲对齐（VAD）
                try:
                    dialogue = Dialogue.from_legacy_script(script)
                    engine = AlignmentEngine(mode="VAD", noise_db=args.silence_db)
                    engine.align_song(dialogue, full_song)
                    external_alignment = _dialogue_to_external_alignment(dialogue)
                    # 导出时间点供用户微调
                    export_timings(dialogue, auto_timings_file)
                    print(f"   自动对齐完成，已导出时间点: {auto_timings_file}")
                except Exception as e:
                    print(f"   自动对齐失败: {e}")
                    print(f"   将回退到TTS模式")
                    external_alignment = None
                    full_song = None

            if external_alignment:
                print(f"\n   对齐结果：")
                print(f"      整曲时长: {external_alignment['total_duration']:.1f}s")
                n_song = external_alignment.get("song_message_count", 0)
                n_narr = external_alignment.get("narration_count", 0)
                narr_in_audio = external_alignment.get("narrations_in_audio", False)
                print(f"      歌词消息: {n_song}条（在歌曲中）")
                if n_narr > 0:
                    if narr_in_audio:
                        print(f"      旁白消息: {n_narr}条（在音频中）")
                    else:
                        print(f"      旁白消息: {n_narr}条（将单独TTS叠加）")
                print(f"\n   时间轴详情：")
                for a in external_alignment["alignment"]:
                    if a.get("is_narration"):
                        narr_tag = " [旁白]" if narr_in_audio else " [旁白TTS]"
                    else:
                        narr_tag = ""
                    print(f"      [{a['index']:02d}] {a['start_s']:.2f}s-{a['end_s']:.2f}s "
                          f"({a['end_s']-a['start_s']:.2f}s) {a['role']}: "
                          f"{a['text'][:25]}...{narr_tag}")
                print(f"\n   如需微调时间点，编辑 {os.path.basename(auto_timings_file)}")
                print(f"      修改后重新运行，加 --timings <file> 参数即可")
        else:
            # ---- 逐文件模式：原有的000.mp3/001.mp3模式 ----
            manual_map = load_manual_audio(CUSTOM_DIR, script["messages"])
            if not manual_map:
                print("  未找到音频文件！将全部使用TTS补充。")
                print(f"     方式1（Suno/妙响整曲）: 将完整歌曲命名为 full.mp3 放入目录")
                print(f"     方式2（逐句配音）: 将音频命名为 000.mp3, 001.mp3 放入目录")
            else:
                print(f"  已加载 {len(manual_map)}/{len(script['messages'])} 个音频：")
                for idx in sorted(manual_map.keys()):
                    role = script["messages"][idx]["role"]
                    txt = script["messages"][idx]["text"][:20]
                    print(f"    [{idx:02d}] {os.path.basename(manual_map[idx])} -> {role}: {txt}...")
                missing = [i for i in range(len(script["messages"])) if i not in manual_map]
                if missing:
                    print(f"  以下消息无音频，将用TTS补充: {missing}")
            originals = patch_tts_with_manual(manual_map, TTS_DIR, script["messages"])

    # ---- 5. 调用 build_gospel_video 执行完整流水线 ----
    plan_path = os.path.join(ASSETS_DIR, f"plan_{project_name}.json")
    try:
        from gospel_automator import build_gospel_video
        print("\n" + "=" * 60)
        print("开始执行完整流水线")
        print("=" * 60)
        result = build_gospel_video(
            script_path=script_json,
            plan_path=plan_path,
            draft_root=args.draft_root,
            export_video=args.export,
            force_speaker=None,
            plan_only=args.plan_only,
            speedup=args.speed if not external_alignment else 1.0,  # 整曲模式不做变速
            external_alignment=external_alignment,
            force_rebuild_plan=True,  # 自动化入口始终重建plan
            asset_dir=CUSTOM_DIR,
        )
    finally:
        # 恢复原函数
        if originals:
            import gospel_automator as ga
            ga._tts_segment, ga._speedup_audio = originals

    # ---- 6. 输出结果 ----
    print("\n" + "=" * 60)
    if result.get("ok"):
        print("完成！")
    else:
        print("有错误：")
        for e in result.get("errors", []):
            print(f"   - {e}")
    print("=" * 60)
    if result.get("draft_path"):
        print(f"  剪映草稿: {result['draft_path']}")
    if result.get("chat_video"):
        print(f"  聊天视频: {result['chat_video']}")
    print(f"  Plan文件: {plan_path}")
    print(f"  剧本文件: {script_json}")
    if args.audio_mode == "manual":
        print(f"  音频目录: {CUSTOM_DIR}")
    missing = result.get("missing_assets", [])
    if missing:
        print(f"\n有 {len(missing)} 项素材需要人工补充：")
        for m in missing:
            print(f"    - [{m.get('type','?')}] 消息#{m.get('index','?')}: {m.get('detail','')}")

    # 提示手工处理的旁白
    try:
        with open(plan_path, "r", encoding="utf-8") as f:
            plan_data = json.load(f)
        manual_narrs = plan_data.get("manual_narrations", [])
        if manual_narrs:
            print(f"\n旁白手工处理提示：")
            print(f"   以下 {len(manual_narrs)} 条旁白未自动配音，请在剪映中手动添加：")
            for idx in manual_narrs:
                msg = plan_data["messages"][idx]
                print(f"    - [{idx:02d}] {msg['start_s']:.1f}s-{msg['end_s']:.1f}s: {msg['text'][:30]}...")
    except Exception:
        pass

    if result.get("ok") and result.get("draft_path"):
        print("\n打开剪映专业版即可在草稿列表中看到该项目。")
        if args.export:
            print(f"   导出视频: {args.export}")

    # 清理临时剧本文件？不，保留给用户参考
    # try: os.remove(script_json)
    # except: pass


if __name__ == "__main__":
    main()
