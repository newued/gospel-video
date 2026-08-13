# -*- coding: utf-8 -*-
"""对话文本自动解析器 — 兼容转发层（v5 架构）。

本文件保持旧 API 兼容，实际逻辑已拆分到 engine 包：
  engine.parser        文本结构解析（[A]xxx / A: xxx / 续行 / 音色建议行剥离）
  engine.emotion       情绪检测（text -> emotion）
  engine.role_mapper   角色命名 + 音色推断（标签 -> 角色名 + speaker_id）

对外保留旧入口：
  parse_dialog_text / parse_dialog_file
  generate_suno_prompt / generate_miaoxiang_prompt
  NARRATOR_ROLES / VOICE_KEYWORDS / DEFAULT_ROLE_NAMES / EMOTION_KEYWORDS / detect_emotion
"""
import os
import json
from datetime import datetime
from dataclasses import asdict

from engine.models import Message
from engine.parser import (
    RawMsg, parse_text, parse_file,
    extract_voice_hints, infer_bgm_query,
)
from engine.emotion import detect_emotion, EMOTION_KEYWORDS
from engine.role_mapper import (
    map_roles, DEFAULT_ROLE_NAMES, NARRATOR_ROLES, VOICE_KEYWORDS,
)

# 兼容旧名：原 parse_voice_hints 改名为 extract_voice_hints
parse_voice_hints = extract_voice_hints

# 旧格式兼容：转发层使用的默认音色（与 Dialogue.speaker 默认一致）
_DEFAULT_SPEAKER = "zh_male_huoli"


def _message_to_dict(m: Message) -> dict:
    """Message -> 旧格式 dict（保留 audio/visual 子对象）。

    旧格式消息字段：role/type/text/emotion/is_narration；
    同时携带 id/speaker（原始标签）以及 audio/visual 子对象，供下游使用。
    """
    return {
        "id": m.id,
        "speaker": m.speaker,
        "role": m.role,
        "type": m.type,
        "text": m.text,
        "emotion": m.emotion,
        "is_narration": m.narration,
        "effects": m.effects,
        "audio": asdict(m.audio),
        "visual": asdict(m.visual),
    }


def parse_dialog_text(raw_text: str, title: str = None, project_name: str = None) -> dict:
    """解析原始对话文本为剧本 JSON 格式（旧格式兼容）。

    内部流程：parser.parse_text + role_mapper.map_roles + emotion.detect_emotion。
    """
    raws = parse_text(raw_text)
    if not raws:
        raise ValueError("未能解析出任何对话消息，请检查输入格式（支持 [A] xxx 或 A: xxx）")

    hints = extract_voice_hints(raw_text)
    messages, role_speakers = map_roles(raws, hints, default_speaker=_DEFAULT_SPEAKER)

    # 标题和项目名
    if not title:
        title = messages[0].text[:20] if messages else "福音对话"
    if not project_name:
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        project_name = f"Gospel_{ts}"

    bgm_query = infer_bgm_query(raw_text)

    # 重建标签映射（{字母标签: 角色名}），供 Suno 提示词生成反查；
    # 中文标签角色其角色名即标签，无需进入映射
    role_label_map = {}
    for m in messages:
        if m.narration:
            continue
        if m.speaker != m.role:
            role_label_map[m.speaker] = m.role

    script = {
        "title": title,
        "project_name": project_name,
        "resolution": 1080,
        "speaker": _DEFAULT_SPEAKER,
        "bgm_query": bgm_query,
        "role_speakers": role_speakers,
        "messages": [_message_to_dict(m) for m in messages],
        "_role_label_map": role_label_map,  # 用于Suno提示词生成
    }
    return script


def parse_dialog_file(file_path: str, title: str = None) -> dict:
    """从文本文件解析对话（兼容层，逻辑与旧版一致）。"""
    if not os.path.exists(file_path):
        # 尝试相对于samples目录查找
        samples_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
        sample_path = os.path.join(samples_dir, file_path)
        if os.path.exists(sample_path):
            file_path = sample_path
        else:
            print(f"\n❌ 文件不存在: {file_path}")
            print(f"\n💡 使用方式：")
            print(f"   1. 创建对话文本文件，例如 my_dialog.txt，内容格式：")
            print(f"      [A] 你好")
            print(f"      [B] 你好啊")
            print(f"      [旁白] 片刻后")
            print(f"      [A] 哈哈哈")
            print(f"   2. 或者直接用命令行传文本：")
            print(f'      python scripts/gospel_dialog.py -T "[A]你好\\n[B]你好\\n[A]哈哈哈" -t "标题"')
            print(f"   3. 或者使用自带示例：")
            print(f"      python scripts/gospel_dialog.py -f samples/emperor_wisdom.txt")
            print()
            available = [f for f in os.listdir(samples_dir) if f.endswith(('.txt', '.json'))] if os.path.exists(samples_dir) else []
            if available:
                print(f"📂 可用示例文件（samples/目录）：")
                for f in available:
                    print(f"   - {f}")
                print()
            raise FileNotFoundError(f"文件不存在: {file_path}")
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    name = os.path.splitext(os.path.basename(file_path))[0]
    return parse_dialog_text(text, title=title or name, project_name=name)


def generate_suno_prompt(script: dict, style: str = "gospel_funk") -> str:
    """根据剧本生成 Suno 可用的提示词。

    严格规则：
      1. 开头第一行是英文曲风Prompt
      2. 第二行[Verse]
      3. 普通对话逐条 [Label] 原文（文字标点符号一丝不改）
      4. 旁白消息跳过（Suno不唱旁白）
      5. [Outro] 和 [End] 各占一行
      6. 杜绝内容重复
    """
    style_prompts = {
        "gospel_funk": "Gospel-infused funk, dual powerful black male lead vocals, raspy soulful vocal texture, intricate melismatic riffs and gospel ad-libs, conversational call and response delivery, lush gospel choir backing harmonies, tight slap bassline, chicken scratch rhythm guitar, punchy brass section, play full dialogue ONLY ONCE, never loop or repeat content",
        "ballad": "Emotional piano ballad, male vocal duet, tender and heartfelt delivery, soft strings backing, gentle piano accompaniment, cinematic build, play full dialogue ONLY ONCE, never loop",
        "rap": "Comedy hip-hop rap, dual male MCs, playful flow, boom bap beat, funky bassline, witty delivery, ad-libs, turntable scratches, play full dialogue ONLY ONCE, never loop",
        "pop": "Upbeat C-pop, catchy male vocals, bright synth production, bouncy rhythm, playful comedic tone, chorus hooks, play full dialogue ONLY ONCE, never loop",
    }
    style_prompt = style_prompts.get(style, style_prompts["gospel_funk"])

    # 角色标签映射（反查：角色名 -> 原A/B标签）
    label_map = script.get("_role_label_map", {})
    name_to_label = {v: k for k, v in label_map.items()}

    # 保留每个A/B角色最后一次出现的原始标签，确保[A][B]正确
    # 注意：只输出普通对话角色，旁白跳过
    lines = [style_prompt, "[Verse]"]
    for msg in script["messages"]:
        role = msg["role"]
        # 旁白消息在Suno中跳过（不唱旁白）
        if role in NARRATOR_ROLES or msg.get("is_narration"):
            continue
        label = name_to_label.get(role, role)
        # 严格保留原始文本，不做任何修改
        text = msg.get("text", "")
        lines.append(f"[{label}] {text}")
    lines.append("[Outro]")
    lines.append("[End]")
    return "\n".join(lines)


def generate_miaoxiang_prompt(script: dict, style: str = "gospel_funk") -> str:
    """生成妙响音乐提示词（与Suno格式完全一致，都是带人声的歌曲生成）。"""
    return generate_suno_prompt(script, style=style)


if __name__ == "__main__":
    test = """
音色建议：A干净男生 B中年男人 旁白广播男音
[A] 大师，最近生意很不好，有什么方法可以改变吗？
[B] 问你一个问题
[B] 现在有两只鬼要吃掉你
[B] 一只红鬼 一只绿鬼
[B] 红鬼一箭就可以射死 绿鬼需要两箭
[B] 现在你手上有两只箭
[B] 你该怎么办？
[旁白]一分钟后
[A] 先射绿鬼一箭，谁不听话最后那一箭射谁
[B] 本想教你平庸之道，却不曾想你却悟出了帝王之术
[B] 贫道佩服
"""
    script = parse_dialog_text(test, title="帝王之术")
    # 不输出内部字段
    out = {k: v for k, v in script.items() if not k.startswith("_")}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print("\n" + "=" * 60)
    print("Suno 提示词：")
    print(generate_suno_prompt(script))
