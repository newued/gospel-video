# AGENTS.md — 给 AI 编码代理的快速上手指南

> 本文件面向 Codex / Claude Code / Cursor 等命令行 AI 代理。WorkBuddy 用户请读 `SKILL.md`，人类用户请读 `README.md`。

## 这是什么

把「对话剧本 + 一段配音音频」自动变成**剪映专业版可编辑草稿**的生成器：
微信聊天界面逐条弹出气泡（A=左/白/对方，B=右/绿/自己）+ 头像 + 表情贴纸 + 画面特效 + 音效，与语音毫秒级对齐（faster-whisper 词级时间戳 + 全局 DP 对齐；唱词反复/重录段按卡拉OK式独立弹出，时间轴连续）。

**核心入口：`scripts/ab_generator.py`**（纯 Python CLI，无平台绑定）。

## 快速开始（代理可直接执行）

```bash
pip install -r requirements.txt          # faster-whisper / imageio / Pillow
python scripts/ab_generator.py --dialog-json <dialog.json> --audio-mp3 <voice.mp3> [--output-draft out/]
```

- 输入：AB 对话剧本 JSON（`[{"role":"A","content":"..."}, ...]`，中文角色名自动映射：先出现的→A/左/白，后出现的→B/右/绿）+ 已配音 MP3。
- 输出：剪映草稿目录（默认 `%LOCALAPPDATA%/JianyingPro/User Data/Projects/com.lveditor.draft/ab_dialog_<时间戳>`），用户打开剪映即可编辑。
- 首次运行会联网下载 faster-whisper small 模型（约 460MB）。

## 素材决策工作流（重要契约）

脚本**不做**贴纸/音效/特效的关键词硬匹配——这些语义决策由 AI 代理在对话中完成：

1. 先导出中间 plan：`--export-plan plan.json`
2. 读 `plan.json`，在 `sticker` / `effect` / `audio(音效)` 轨的 `material` 字段按剧情语义填素材：
   - 贴纸：`assets/emojis/` 下的文件名（如 `angry_01.png`）
   - 特效：剪映内置特效名（已验证可解析：`预警`/`裂开了`/`哈哈弹幕`/`凄凉`/`冲刺`/`颤抖` 等）
   - 音效：`assets/sfx/` 下的文件名
3. 重新生成（秒出，不重跑 ASR）：`python scripts/ab_generator.py --plan-json plan.json`

`enrich_plan.py` 不在仓库内——它是代理的辅助脚本示例；代理可直接修改 plan.json 或自建脚本（按文本内容映射即可，重录片段会自动继承同句决策）。

## 关键文件

| 文件 | 作用 |
|---|---|
| `scripts/ab_generator.py` | ★ 主入口（auto 模式 + plan 模式） |
| `vendor/pyJianYingDraft/` | vendored 剪映草稿生成库（MIT，勿改） |
| `assets/blank_template/` | 剪映 5.9 空白草稿模板 |
| `assets/emojis/` `assets/sfx/` | 贴纸/音效素材（网络素材，版权归原作者，仅供个人学习） |
| `docs/AB_DIALOG_GUIDE.md` | 完整使用说明（输入格式/校验/轨道结构/FAQ） |
| `README.md` / `SKILL.md` | 人类 / WorkBuddy 入口 |

## 注意事项

- 需剪映专业版 5.9+（Windows/macOS）；脚本只生成草稿，不启动剪映。
- `vendor/` 是 pyJianYingDraft 内置副本；自定义路径可用环境变量 `PYJYD_VENDOR` 覆盖。
- `scripts/gospel_automator.py`（完整流水线：TTS→渲染→组装）需要 [jianying-editor](https://github.com/newued/jianying-editor)（环境变量 `JY_SKILL_ROOT` 指向其根目录），**缺失时仅该入口降级，AB 模式不受影响**。
- 改动 `ab_generator.py` 的 `asr_align` 时间轴逻辑后，建议跑一遍 `python scripts/ab_generator.py --dialog-json samples/xxx.json --audio-mp3 ...` 验证草稿可正常生成。
