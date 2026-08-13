---
name: gospel-video
description: 福音吐槽风格微信聊天对话视频自动生成流水线（v6 工程化升级版）。输入对话文本（支持[A][B][旁白]格式）或剧本JSON，自动完成：多角色配音（自动TTS/手动音频/Suno福音歌曲）→ 微信聊天界面逐条弹出渲染（旁白居中半透明样式）→ 下半屏居中表情包贴纸+克制音效+随机转场 → 剪映草稿组装。v5 支持 AB 双人对话模式（已配音 MP3 + faster-whisper 毫秒对齐）。v6 新增：①对账闸门(QA Gate)——生成前逐项核对台词/配音/气泡/表情包/音效/连续性，把错误拦在花算力之前；②音画同步升级——默认整曲对齐优先 ASR(faster-whisper)毫秒级精度，失败安全降级 VAD；③资产沉淀——每次运行归档剧本/音乐推荐到 assets/library/ 跨项目复用；④复刻模式——用拆解提示词把爆款聊天视频拆成剧本JSON再生成；⑤音乐策略——长叙事用 Mureka、短快节奏用 Suno、魔性 BGM 预设；⑥画面特效标记——剧本里写 [[特效:烟花]] 等标记，离线自动套用剪映真实画面/综艺特效（开幕/烟花/震动/冲刺/啊啊啊啊等 50+ 模板）。零依赖离线核心，AI 视频渲染(Flova/Seedance)作为可选扩展。触发词：福音视频、吐槽视频、聊天对话视频、微信聊天记录、剪映草稿、生成聊天视频、福音短视频、对话视频、AB对话视频、已配音音频草稿、复刻爆款、对账闸门、音画同步、资产沉淀、画面特效、微信特效、剪映特效、综艺特效。
---

# 福音吐槽视频流水线（gospel-video）v6

福音风格微信聊天对话视频自动化工作流。**支持直接粘贴对话文本**（`[A]`/`[B]`/`[旁白]` 格式），自动识别角色、旁白、情绪、音色建议，一键产出剪映草稿。

> **v6 工程化升级（2026-08）**——本版围绕「**闸门思维**」与「**资产沉淀**」两条主线升级，灵感来自爆款复刻工程化实践：
> 1. **对账闸门（QA Gate）**：在 Timeline 构建后、渲染前自动逐项核对（台词↔配音时间↔气泡↔表情包↔音效↔连续性），把错误拦在花算力生成之前。默认不阻断（保留离线容错），可 `--strict-qa` 强制中断。
> 2. **音画同步升级**：整曲对齐默认优先 **ASR（faster-whisper 毫秒级）**，缺失依赖时自动降级 VAD 静音检测——解决传统"音画不同步、手动拉音轨"痛点。
> 3. **资产沉淀**：每次运行自动归档剧本/角色/情绪分布/音乐推荐到 `assets/library/`，做得越多可复用底子越厚。
> 4. **复刻模式**：用内置拆解提示词把一条爆款聊天视频拆成剧本 JSON，再走本流水线生成（见下文「复刻模式」）。
> 5. **音乐策略**：长叙事→Mureka、短快节奏→Suno、魔性 BGM 预设内置（`engine/library.py`）。
> 6. **画面特效标记**：剧本里写 `[[特效:烟花]]` / `[[特效:震动@0:03|2s]]` 等标记，流水线自动归一化为剪映真实模板名（如「震动」「灵魂出窍_II」「冲刺_II」），离线写入剪映 `effect` 轨（基于 jianying-editor skill 的 `add_effect_simple`）。无需手动套用，导出草稿即所见（详见下文「画面特效标记」）。
> AI 视频渲染（Flova/Seedance 2.5 等需外部付费 API）本轮仅文档化可选扩展，**不破坏零依赖离线核心**。

> 架构：v4 起引擎化，内部统一操作 `Dialogue`/`Timeline` 两个核心数据模型，经 DAG 流水线（解析→角色/情绪→音频规划→对齐→视觉规划→时间轴→**对账闸门**→渲染→**资产归档**→剪映组装）串接，各域模块可独立替换（换 TTS/换渲染器/换素材库不动核心）。架构契约见 `docs/ARCHITECTURE_v5.md`。v6 新增节点为 `QAGateNode`/`LibraryNode`（见 `scripts/engine/pipeline.py`）。用户侧 CLI 与用法不变。
>
> v5 新增 **AB 双人对话模式**：提供已配音完整 MP3 + AB 对话 JSON，用 faster-whisper ASR 自动对齐逐句时间戳，直接产出剪映 5.9 草稿（无需 TTS、不切音频、气泡不重叠）。入口：`scripts/ab_generator.py`。

## AB 双人对话模式（v5，已配音音频 + 大模型语义决策）

适用于已有一整段双人配音音频（人工录制/其他工具生成）的场景，无需 TTS。**执行方式：决策由你（大模型）在对话中完成，脚本只做机械执行**——不要只把 `python scripts/ab_generator.py --dialog-json ... --audio-mp3 ...` 丢给用户，那会走纯规则引擎、跳过大模型的语义判断（表情包/音效匹配质量明显下降）。

### 输入素材（用户提供）

| 文件 | 必填 | 说明 |
|---|---|---|
| `dialog.json` | ✅ | `[{"role":"A","content":"..."},{"role":"B","content":"..."}]`，支持 A/B 或中文角色名（自动映射） |
| `voice.mp3` | ✅ | 完整双人配音 MP3（整段使用，不切分） |
| `A.png`/`B.png` | 可选 | 左右矩形圆角头像（蒙版圆角 50，非圆形），放 dialog.json 同目录；也支持中文角色名头像（如 `贾总.png`） |
| `BG.png` | 可选 | 聊天背景图铺满全屏 |

> 内置缺省（无需用户准备）：贴图库 `assets/emojis`、音效库 `assets/sfx`、剪映5.9空白模板 `assets/blank_template`、草稿输出到**剪映默认草稿目录**（Windows：`%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft`，macOS：`~/Movies/JianyingPro/...`），打开剪映即可在草稿箱看到。剪映草稿由内置 pyJianYingDraft 生成，无需搭配其他 skill（若需检查草稿完整性可另加载 jianying-editor skill）。
> 完整使用说明见 [docs/AB_DIALOG_GUIDE.md](docs/AB_DIALOG_GUIDE.md)（环境准备/输入格式/8条校验/输出轨道结构/FAQ）。

### 你的工作流（五步，决策由你做，脚本机械执行）

1. **读对话**：读取 `dialog.json`，理解每条台词的语义与情绪（A/B 双方各说了什么、槽点/笑点在哪）。支持中文角色名（如"贾总"/"王工"），脚本自动映射到 A/B。
2. **读素材库**：读取 `assets/emoji_scenes.md`（161 张表情包场景库，每条含 **核心情绪/标签/画面描述**）与 `assets/sfx/` 音效库，建立「台词 → 素材」候选映射。
3. **逐条语义决策**：对每条消息判断是否需要表情包/音效，并挑选素材（决策规则见下）：
   - **表情包**：需要 → 从 emoji_scenes.md 挑**画面描述最贴合台词语义/情绪**的一张，`material` 填文件名（如 `一起哈皮.jpg`）；不需要或库中无合适 → `material` 置空串。
   - **音效**：需要 → 从 `assets/sfx/` 挑贴合情绪的，`material` 填文件名；平淡陈述句不配。
4. **写中间 JSON**：按下方 schema 生成 `plan_<name>.json`（决策结果写入 `sticker`/`audio(sfx)` 段的 `material`）。
5. **脚本机械执行**：
   ```bash
   # 推荐：同时提供 dialog.json 和 voice.mp3，脚本自动做 ASR 对齐
   python scripts/ab_generator.py --plan-json plan_<name>.json --dialog-json dialog.json --audio-mp3 voice.mp3
   
   # 或仅用 plan-json（时间戳需手动准确填写，不推荐）
   python scripts/ab_generator.py --plan-json plan_<name>.json
   ```
   脚本完成：素材解析（文件名自动在 `assets/emojis`/`assets/sfx` 下定位）→ faster-whisper small ASR 对齐逐句时间戳（简体 prompt 强制简体输出 + **词级时间戳全局 DP 对齐**，抗转写口误/繁简/重复段；唱词反复/重录段自动并入对应句时间轴，重叠窗口由分层轨承载，详见 `ab_generator.asr_align`/`_assign_layers`）→ 数据适配（气泡 A 左 B 右、displayStart=audio_start、displayEnd=audio_end）→ 剪映 5.9 草稿填充（重叠消息自动分到 独立文本/贴图/头像/特效轨）→ 输出 `执行完成！剪映草稿输出路径：<output_draft_dir>`。
6. **验证并汇报**：确认草稿已生成到剪映草稿目录，向用户说明产物位置与可调整项。

### 素材决策规则（你判断时遵循）

- **表情包**：以台词的**核心情绪/笑点/槽点**为准，从 emoji_scenes.md 选画面描述最匹配的一张；强情绪（惊喜/愤怒/悲伤）优先 `happy_01`/`surprise_01`/`angry_01`/`sad_01` 等基础表情，场景梗图优先匹配具体语境（如「你放屁.jpg」匹配反驳类台词）。
- **音效**：强喜剧标记（哈哈哈/卧槽/绝了/？？？/！！！）、强情绪（angry/surprise）才配；平淡陈述句不配；同一角色连续说话避免音效连发。
- **素材引用**：`material` 填文件名即可，脚本自动解析（无需绝对路径）；不存在的素材不会中断流程，只打印中文提示。

### 硬约束（脚本强制，JSON 中的值不得覆盖）

- **音画同步**：气泡/贴图显示时间由 ASR 对齐的 `audio_start`/`audio_end` 决定（`display_start=audio_start`、`display_end=audio_end+0.4s`，相邻重叠自动顺延）——JSON 里的 `startTime`/`endTime` 落草稿时仍按该规则重算
- **贴图尺寸位置**：全宽贴底（calc_sticker_layout）——目标高度=画布高×2/5(768px)，等比缩放计算宽度（disp_w = 768 × 原始宽/原始高）；scale_x = 显示宽度/舞台宽(1080)，scale_y = scale_x（**必须相等，否则变形**）；scale 上限 5.0（小图钳制后居中）；超高保护（目标高>画布高95%时回缩）；水平居中（transform_x=0）、垂直贴底（ty<0，底边距画布底5%）；与气泡位置无关——JSON 中 scale/position 不参与计算，一律由脚本读素材宽高重算。**坐标系**：(0,0)=居中，y值越大越往上，y值越小越往下；scale_x=显示宽/画布宽(100%=画布宽)
- **plan.json 规范化**：`--plan-json` 运行时脚本自动读取每个 sticker 段素材的真实宽高，按上述规则重算 `scale`/`position` 并**写回 plan.json 文件**（保证 plan 与剪映草稿数值一致，便于核对素材大小）；`material` 字段写回原名不落绝对路径

**中间 JSON schema**（结构示意，完整字段说明见 [docs/AB_DIALOG_GUIDE.md](docs/AB_DIALOG_GUIDE.md)）：

```json
{
  "meta": {"title": "视频标题", "resolution": [1080, 1920], "fps": 30, "aspect_ratio": "9:16",
           "bg_color": "#000000", "chat_bg_color": "#f5f5f5", "render_quality": "high"},
  "characters": {
    "A": {"name": "老业主 王哥", "avatar": "assets/avatars/wangge.jpg", "avatar_border": "circle",
          "bubble_color": "#ffffff", "text_color": "#000000", "side": "left",
          "text_font": "PingFangSC-Regular", "text_font_size": 32},
    "B": {"name": "...", "bubble_color": "#95ec69", "side": "right"}
  },
  "tracks": [
    {"type": "video",   "segments": [{"startTime": 0, "endTime": 39, "material": "背景图路径"}]},
    {"type": "audio",   "segments": [{"startTime": 0, "endTime": 39, "material": "配音.mp3"}]},
    {"type": "text",    "segments": [{"startTime": 1.0, "endTime": 2.5, "material": "曾小黑在吗？", "speaker": "A",
                                      "style": {"size": 32, "color": "#000000"}, "animation": {"type": "pop_in"}}]},
    {"type": "sticker", "segments": [{"startTime": 1.5, "endTime": 2.1, "material": "贴图路径或空", "hint": "建议：happy 类贴图"}]},
    {"type": "audio",   "segments": [{"startTime": 1.0, "endTime": 2.5, "material": "音效.mp3", "volume": 0.5}]}
  ],
  "transitions": [{"type": "dissolve", "duration": 0.3, "easing": "ease-in-out"}]
}
```

> 典型改法：改 `characters` 的 `bubble_color`/`text_color`/`avatar`，改 text 段 `material` 文案或 `style`，按 hint 给 `sticker`/`audio` 段补 `material` 后，用 `--plan-json` 重跑即可（秒出，不重跑 ASR）。

## 快速开始（推荐：一键生成）

直接粘贴对话文本即可，不需要手动写JSON：

```bash
# 方式1：对话写在文本文件里
python scripts/gospel_dialog.py -f my_dialog.txt

# 方式2：直接在命令行传文本（\n 换行）
python scripts/gospel_dialog.py -T "[A]你好\n[B]你好啊\n[旁白]一分钟后\n[A]哈哈哈"

# 方式3：生成Suno福音歌曲提示词，手动生成音频后再合成
python scripts/gospel_dialog.py -f my_dialog.txt -m suno
# → 复制提示词去Suno生成 → 下载音频放入指定目录
python scripts/gospel_dialog.py -f my_dialog.txt -m manual
```

## 复刻模式（Replicate Mode）🆕 v6

把一条爆款聊天视频「复刻」成你的版本。核心思路（来自爆款复刻工程化实践）：**先拆成结构化剧本 JSON，再喂给本流水线生成**——不靠一条神级提示词，而是工程化拆解。

**在 AI 助手（Codex / WorkBuddy）中**：把参考视频丢给大模型，要求按 `engine/qa.py` 的 `VIDEO_DECOMPOSE_PROMPT` 契约输出剧本 JSON（角色/台词/emotion/旁白/BGM 关键词）。可直接调用：

```python
from engine.qa import build_replicate_prompt
print(build_replicate_prompt("参考视频：老板让员工周末加班的反怼剧情"))
```

输出 JSON 后保存为 `my_replica.json`，用**模型直通模式**渲染（大模型已决策 emotion，脚本机械执行）：

```bash
# 1. 先预览素材匹配（对账闸门会在此自动运行）
python scripts/gospel_automator.py my_replica.json --plan-only
# 2. 满意后生成完整剪映草稿
python scripts/gospel_automator.py my_replica.json
```

> 复刻模式与 AB 模式区别：AB 模式需要你**已提供完整配音 MP3**；复刻模式只需**参考视频的剧本结构**，配音由本流水线重新生成（TTS 或 Suno 整曲），适合"换个角色/换个产品再拍一条"。

## 画面特效标记（Scene Effects）🆕 v6

在对话文本里用 `[[特效:名]]` 标记，让某条消息在对应时间点自动套用剪映画面/综艺特效。**离线、全自动**——流水线把中文名归一化为剪映真实模板名，写入剪映 `effect` 轨，导出草稿即所见，无需手动套。

### 语法

```
[[特效:模板名]]             # 作用在该条气泡时间段，时长取推荐默认
[[特效:模板名@时间点]]       # 指定起始时间（0:03 / 3s / 3000ms）
[[特效:模板名@时间点|时长]]   # 同时指定时长（2s / 2000ms）
```

可一行内多个标记、夹在文本中间：

```
[A]大师快看[[特效:烟花@0:03|2s]]这烟花好看[[特效:震动]]
[B]贫道佩服！[[特效:开幕]]
[旁白]一分钟后[[特效:时间停止]]
```

### 内置对照（口语词 → 剪映真实名，`engine/effects_catalog.py`）

下面的词大多**直接可用**（剪映素材库 `video_scene_effects.csv` 实测命中）。带括号的是你写的词与剪映实际名不同、已自动修正的：

| 分类 | 可用特效（部分，共 50+） |
|---|---|
| 基础画面特效 | 开幕、开幕_II、繁星点点、冲击波、抖动、荧光扫描、荧光星河、泡泡、心跳、灵魂出窍_II、彩虹射线、花火、水波纹、烟雾、渐渐放大、左右摇晃、梦蝶、震荡、摇摆、摇摆_II、闪动、色差、RGB描边、幻影、毛刺、幻术摇摆、迷离、几何图形、视频分割、彩色负片、定格闪烁、横纹故障_II、故障 |
| 综艺特效 | 满屏问好、凄凉、夸夸弹幕、震动（你写的"振动"）、预警、飞速计算、颤抖、冲刺、冲刺_II、冲刺_III、啊啊啊啊、哈哈弹幕、变形了、时间停止 |

> 未在表中的词会按原名透传，交给剪映本地枚举匹配；若剪映无此模板，QA 闸门会提示你手动替换相近特效。

### 实现要点（给维护者）

- 标记解析：`engine/parser.py` 的 `parse_text` 用 `effects_catalog.parse_effect_tags` 剥离 `[[特效:...]]`（保留纯对话文本）。
- 归一化：`engine/effects_catalog.normalize_effect` 做口语→剪映真实名映射；`visual_planner._plan_scene_effect` 消费并写入 `VisualSpec.scene_effect`。
- 草稿写入：`gospel_automator._assemble_draft` 调 jianying-editor 的 `project.add_effect_simple(名, start, dur, "EffectTrack")`，离线建 `effect` 轨。
- 校验：QA 闸门对未知特效名给 warning 提示。

## 聊天表情包：剪映原生贴纸自动匹配 🆕

在剪映草稿组装阶段（`_assemble_draft` 逐条消息循环），除了聊天卡片内已烘焙的本地表情图，还会**自动从剪映本地 artistEffect 贴纸库里挑贴纸叠到 `StickerTrack` 轨道**。选贴逻辑复用 jianying-editor skill 的离线贴纸索引 + 规则化情感/关键词匹配（无需联网、无需 LLM；检测到 `antigravity-api-skill` 时自动升级为 LLM 选贴）。

### 行为
- 对每条带文本/情绪的消息，调 `project.select_sticker_for_chat(text, top_k=1)` 选 1 张；
- 仅叠加「可选（`selectable=True`）、有真实 `path`、非对话框类」的贴纸——占位缓存贴纸（无真名、只能按 ID 添加）和纯对话框框贴纸会被跳过，避免与聊天卡片重复；
- 坐标：**竖屏 1080×1920** 坐标系，贴纸落于下半区（`transform_y=0.35`），按角色左右分布（A 左 / B 右），`scale=0.5`；
- 时间窗对齐消息 `start_s→end_s`（最短 1.5s，最长 4.0s）。

### 开关与默认
- 默认开启：`scripts/gospel_automator.py` 顶部 `NATIVE_STICKERS_ENABLED = True`；
- 想退回"仅卡片内烘焙表情图"：设为 `False` 即可，不影响其它组装；
- **输出分辨率**：gospel-video 所有草稿 / 聊天视频固定竖屏 **1080×1920（9:16）**，由 `CANVAS_WIDTH` / `CANVAS_HEIGHT` 常量单一来源控制（`_assemble_draft` 与 `build_gospel_video --plan-only` 两处均引用），不会被某条路径改回横屏。

### 实现要点（给维护者）
- 能力来自 jianying-editor 的 `StickerOpsMixin`（已混入 gospel 实际 import 的 `JyProject`）：`add_sticker()` 先排队，`project.save()` 末尾 `_inject_stickers()` 把完整 39 字段贴纸 material/segment 注入 `draft_content.json`（绕开 pyJianYingDraft 自带 `StickerSegment` 字段不全导致空白的问题）。
- 选贴无结果（聊天文本无情绪/关键词命中）时静默跳过，该消息不加叠层贴纸。
- 当前本地可选贴纸数量有限（取决于你剪映草稿/Cache 里真实用过的贴纸）；索引越多，选贴越丰富。可用 jianying-editor 的 `cloud_manager.mget_item` 补全占位缓存贴纸的真名以扩展候选。

## AB 模式：贴纸 / 音效 / 画面特效由大模型在 plan 决策（不再关键词硬匹配）

AB 双人对话模式（`ab_generator.py`）**不做任何基于文字的关键词硬匹配**。贴纸、音效、画面
特效的语义选择全部由你（大模型）在对话中完成，并写入中间 JSON（plan）的对应轨道，脚本只
机械执行：

- **贴纸（sticker 轨）**：`material` 填 `assets/emojis/` 下的表情图文件名（如 `少废话.webp`）；
  不需要则留空串。AB 模式用你自己的表情图，**不再注入剪映原生 `artistEffect` 贴纸**
  （原 `_inject_native_stickers_ab` 已移除；若需要剪映原生贴纸自动匹配，请用模型脚本路径
  `gospel_automator` / `JyProject`，见上文「聊天表情包：剪映原生贴纸自动匹配」）。
- **音效（音效轨）**：`material` 填 `assets/sfx/` 下的音效文件名；不需要则留空串。
- **画面特效（effect 轨）**：`material` 填剪映特效 identifier（如 `震动` / `哈哈弹幕` / `凄凉`）；
  不需要则留空串。脚本经 `_resolve_video_effect` 把中文名解析为剪映 `VideoSceneEffectType`
  枚举并写入 `EffectTrack`。**注意**：这里只做"名字→枚举"的解析，不做"文字→情绪→特效"
  的推理——选哪个特效由你在 plan 里决定。

### 落盘
- `fill_draft_from_plan`（plan 模式，推荐）与 `fill_draft`（auto 模式）均直接消费 plan 的
  sticker / 音效 / effect 轨；某条消息对应段 `material` 为空串时跳过该轨道，并产出 `hint`
  提示词（引导你在对话中补写），不中断流程。
- 未决策段的 `hint` 仅作自然语言引导，例如「建议：为「…」配置剪映内置画面特效」。

### 开关
- `NATIVE_EFFECTS_ENABLED`（默认 `True`）：控制是否消费 plan 的 effect 轨；`False` 则完全不上
  画面特效。贴纸 / 音效的开关等同于"plan 中是否给对应段填了 material"，无需额外开关。

### 实现要点（给维护者）
- `_resolve_video_effect` 复刻 jianying-editor 的 `resolve_enum_with_synonyms`（精确名→大小写
  不敏感→同义词→模糊），并复用 `engine.effects_catalog.normalize_effect` 做口语归一化；
- `effect` 轨是 plan 中间 JSON 的一类 track（`{"type":"effect","segments":[...]}`），与
  text / sticker / audio 轨平级；auto 模式由 `build_plan_json` 导出，plan 模式直接读取；
- effect 段 `material` 为剪映特效 identifier 字符串（如 `"震动"`），非文件路径，故
  `_resolve_asset_path` 不会对 effect 轨做路径解析。

## 对账闸门（QA Gate）🆕 v6

每次生成，流水线会在 Timeline 之后、渲染前自动跑对账闸门，逐项核对：

| 检查项 | 说明 | 级别 |
|---|---|---|
| 台词↔配音时间 | 每条消息时间窗有效（start<end，时长合理） | error/warning |
| 气泡连续性 | 相邻气泡重叠、负时长、长空白、尾留不足 | warning |
| 表情包↔素材 | `sticker`/`overlay_sticker` 指向文件是否真实存在 | warning |
| 音效↔素材 | `sfx` 是否绑定剪映 effect_id | warning |
| 旁白/无音频 | 旁白未自动配音、普通消息 source=none 需人工 | warning |
| 总时长 | 最后一条与 total_duration 尾留 | warning |

控制台会打印 `🛡️ 对账闸门（QA Gate）报告`。**默认不阻断**（保留离线容错，warning 照常生成）；若想严格卡关，加 `--strict-qa`（存在 error 时建议中断生成）。

也可在任意阶段单独调用做审计：
```python
from engine.qa import run_qa, format_qa_report
from engine.models import Dialogue
result = run_qa(dialogue, timeline=ctx.timeline, strict=False)
print(format_qa_report(result))   # result["ok"] / ["abort"] / ["issues"]
```

## 音画同步升级（ASR 优先）🆕 v6

整曲/逐句音频对齐默认走 **ASR（faster-whisper small，毫秒级精度）**，缺失依赖时自动降级到 VAD 静音检测，再不行文本估算。无需手动切换——`AlignmentEngine(mode="AUTO", prefer_asr=True)` 已实现。

```bash
# 手动指定 ASR（需已 pip install faster-whisper）
python scripts/gospel_dialog.py -f my_dialog.txt -m manual   # 整曲优先 ASR
# 若不想用 ASR，强制 VAD：
python -c "from engine.alignment import AlignmentEngine; ..."  # prefer_asr=False
```

> 之前「音画不同步、手动拉音轨」的痛点，现在由 ASR 精确时间轴分析解决（与主流 AI 视频工作流一致）。

## 资产沉淀与音乐策略🆕 v6

- **资产沉淀**：每次运行自动把剧本关键字段（角色/情绪分布/音乐推荐/素材命中/QA 结果）归档到 `assets/library/library_index.json`，跨项目可查可复用。查看：`from engine.library import list_library`。
- **音乐策略**（`engine/library.py` `recommend_music`）：长叙事（消息多/时长久）→ **Mureka + ballad**；短快节奏（默认）→ **Suno + gospel_funk**；BGM 关键词含「魔性/搞笑/meme」→ 优先 Suno 魔性预设。与文章实践结论「长音乐用 Mureka，短音乐用 Suno」一致。
- **魔性 BGM 预设**：`SUNO_PRESETS` 内置 gospel_funk/ballad/rap/pop/catchy_meme 五套英文提示词，可直接用于 Suno/妙响生成。



### 对话文本格式

```
音色建议：A干净男生 B中年男人 旁白广播男音
[A] 大师，最近生意很不好，有什么方法可以改变吗？
[B] 问你一个问题
[B] 现在有两只鬼要吃掉你
[旁白]一分钟后
[A] 先射绿鬼一箭，谁不听话最后那一箭射谁
[B] 贫道佩服！
```

支持的格式：
- `[A]` / `[B]` / `[C]` — 普通角色，按出现顺序自动命名为"年轻人""大师"等
- `[旁白]` — 旁白，自动居中半透明黑底白字样式
- `A: 你好` / `大师：你好` — 冒号格式也支持
- 音色建议行（可选）：自动解析"A干净男生""B中年男人""旁白广播男音"等

## 三种音频模式

| 模式 | 说明 | 命令 |
|---|---|---|
| `auto`（默认） | 自动多角色TTS配音（年轻人=阳光男声，大师=沉稳中年男声，旁白=播音腔） | `-m auto` 或省略 |
| `suno` | 生成Suno/妙响可用的歌曲提示词，暂停等待你生成整曲音频 | `-m suno` |
| `manual` | 加载手动音频：整曲模式（`full.mp3`）自动对齐时间轴，或逐句模式（`000.mp3`）替换TTS | `-m manual` |

### Suno/妙响整曲模式（推荐）

这是最常用的工作流，Suno/妙响生成的是一整首完整歌曲，系统会自动用ffmpeg检测句间静音，把聊天气泡和歌曲演唱对齐：

```bash
# 第一步：生成提示词
python scripts/gospel_dialog.py -f dialog.txt -m suno

# 可选曲风：gospel_funk(福音放克)/ballad(抒情)/rap(说唱)/pop(流行)
python scripts/gospel_dialog.py -f dialog.txt -m suno --suno-style rap
```

执行后输出：
1. Suno/妙响提示词（英文曲风+[Verse]标签+对话+[Outro][End]，旁白跳过不唱）
2. 手动音频目录路径

你需要：
1. 复制提示词到 **Suno** 或 **妙响** 生成歌曲
2. 下载生成的完整音频文件
3. **命名为 `full.mp3`** 放入提示的 `assets/custom_audio/<project_name>/` 目录
4. 执行：
```bash
python scripts/gospel_dialog.py -f dialog.txt -m manual
```

系统会自动：
- 用ffmpeg检测歌曲中的静音段，识别每句歌词的时间点
- 把聊天气泡的弹出时间和歌曲演唱对齐
- 旁白（[旁白]标记的消息）因为Suno提示词里已跳过，自动用TTS生成并叠加在歌曲间隙
- 导出 `timings_<project>.json` 供你微调时间点

**如果自动对齐不准确**：
```bash
# 方法1：调整静音检测灵敏度（值越小越严格，默认-25dB）
python scripts/gospel_dialog.py -f dialog.txt -m manual --silence-db -20

# 方法2：编辑导出的 timings_*.json，手动修正 start_s/end_s 后重跑
python scripts/gospel_dialog.py -f dialog.txt -m manual --timings path/to/timings.json
```

### 逐句配音模式（高级）

如果你不用Suno/妙响，而是自己逐句录制或用其他工具生成音频，可以按序号命名：
- `000.mp3` 对应第一条消息，`001.mp3` 第二条...
- 缺少的文件自动用TTS补充
- 支持mp3/wav/ogg/m4a/aac格式

```bash
python scripts/gospel_dialog.py -f dialog.txt -m manual
python scripts/gospel_dialog.py -f dialog.txt -m manual --audio-dir D:/my_audios/
```

## 所有命令行参数

```
python scripts/gospel_dialog.py [选项]

输入源（二选一）：
  -f, --file PATH       从文本文件读取对话
  -T, --text TEXT       直接传入对话文本（\n 换行）

选项：
  -t, --title TEXT      视频标题
  -p, --project-name    项目名（默认自动生成时间戳）
  -m, --audio-mode      音频模式: auto/manual/suno（默认auto）
  --suno-style STYLE    Suno/妙响曲风: gospel_funk/ballad/rap/pop
  --bgm TEXT            BGM搜索关键词（默认自动推断，整曲模式下不使用）
  --speed FLOAT         全局倍速（默认1.3，整曲模式下自动为1.0）
  --plan-only           只生成plan和聊天视频，不组装草稿
  --audio-dir PATH      手动音频目录
  --timings PATH        加载用户微调过的时间点JSON（整曲模式）
  --silence-db FLOAT    静音检测阈值dB（默认-25，值越小越严格）
  --draft-root PATH     剪映草稿根目录
  --export PATH         导出视频路径（需要剪映≤5.9）
```

## 画面设计

### 普通对话气泡
- 左右分布（类似微信聊天）
- 头像为**矩形圆角蒙版（圆角 50）**，非圆形（`MaskType.矩形` + `round_corner=50`）
- 聊天信息框样式：直接用剪映内置「会话」气泡预设——**A=左侧=对方=白色气泡→「会话79」(effect_id 720192)**、**B=右侧=自己=绿色气泡→「会话80」(effect_id 720206)**，由 `ab_generator.py` 写盘后注入 `materials.effects` 并经文本段 `extra_material_refs` 挂载；开启气泡时关闭原 `TextBackground`（黑字保对比）。参考样式取自用户 2026-08-13 草稿。
- 9种入场动画随机，相邻不重复
- 头像+角色名+气泡
- 覆盖贴纸在下半屏居中区域随机位置出现（8种入场动画+闲置微动）

### 旁白（特殊样式）
- 屏幕居中显示
- 半透明黑色背景（rgba(0,0,0,0.55)）
- 白色斜体文字
- 无头像、无角色名、无贴纸
- 淡入动画为主
- 不插入时间戳

### 贴纸/表情包
- 位置：下半屏居中区域（y≈0.58~0.72，x≈0.35~0.65）
- 尺寸：自适应（长边约38%画布宽度≈410px，小图自动放大）
- 8种入场动画：弹性弹出/掉落弹跳/旋转缩放/钟摆摇摆/左右滑入/脉冲/翻转
- 4种闲置微动：轻微摇摆/小弹跳/心跳/呼吸缩放
- 旁白消息不加贴纸

### 音效（严格克制）
只有以下情况配音效：
- 强喜剧标记（哈哈哈、卧槽、绝了、？？？、！！！）
- 强情绪（angry/surprise）配情绪关键词
- 同人连续说话上一条已配音效则本条跳过
- 平淡陈述句不配音效

## 输出产物

| 文件 | 说明 |
|---|---|
| 剪映草稿目录 | 在剪映草稿根目录下，打开剪映即可看到 |
| `assets/chat_video/<name>_chat.webm` | 微信聊天界面渲染视频 |
| `assets/plan.json` | 中间计划（可手动修改后复用） |
| `assets/_tmp_script_<name>.json` | 自动生成的剧本 |
| `assets/song_prompt_<name>.txt` | Suno/妙响歌曲提示词（suno模式） |
| `assets/custom_audio/<name>/full.mp3` | 放置Suno/妙响生成的整曲音频 |
| `assets/custom_audio/<name>/timings_<name>.json` | 自动检测的时间点（可手动微调） |

## 模型直通模式（在 Codex 等 AI 助手中使用，推荐）

本流水线**不内置任何 LLM 调用，也不需要 API key**。当你在 Codex 等 AI 助手环境中运行本 skill 时，需要智能判断的环节（情绪识别、音色选择、BGM 关键词、贴纸/音效的语义匹配）由**你直接在对话中完成**，脚本只做机械执行（TTS、渲染、剪映组装）。这样纯规则匹配不准确的问题由大模型解决，同时脚本保持零依赖、可离线、可复用。

工作流（三步骤）：

```bash
# 1. 你在对话中阅读用户提供的对话文本，判断每个角色的情绪/音色/贴纸/音效
# 2. 按下方「增强剧本 JSON 格式」写出 JSON 文件（如 assets/_model_script_xxx.json）
# 3. 调用流水线消费：
python scripts/gospel_automator.py assets/_model_script_xxx.json --plan-only
# 审核 plan/聊天视频（素材匹配是否满意）后，去掉 --plan-only 重新生成完整草稿：
python scripts/gospel_automator.py assets/_model_script_xxx.json
```

**模型直通 vs 纯规则**：`-f`/`-T` 文本入口走内置关键词规则（零依赖但准确率有限）；模型直通模式由你（大模型）判断语义，准确率高得多。**凡是你在 AI 助手中使用本 skill，优先用模型直通模式**：你把对话内容用 `-T` 或 `-f` 直接跑，不如先产出增强剧本 JSON 再调 `gospel_automator.py`。

> **v6 提示**：模型直通模式下，脚本在生成前会自动跑**对账闸门**（见上文）。若希望大模型参与"过闸"决策，可在 `--plan-only` 阶段查看 QA 报告，针对 warning（如缺失贴纸/音效）补素材或回写 plan.json，再去掉 `--plan-only` 生成完整草稿——这正契合「闸门思维：错误拦在生成前」。

### 增强剧本 JSON 格式

在基础 JSON（见下方「高级用法」）基础上，`messages` 每条可追加语义字段：

```json
{
  "title": "视频标题",
  "bgm_query": "搞笑 悬疑 国风",
  "role_speakers": {
    "年轻人": "BV056_streaming",
    "大师": "BV701_streaming",
    "旁白": "zh_male_voplvyou"
  },
  "messages": [
    {
      "role": "年轻人",
      "text": "大师，最近生意很不好，有什么方法可以改变吗？",
      "emotion": "sad",
      "sfx": {"effect_id": "", "title": "叹气声"}
    },
    {
      "role": "年轻人",
      "text": "先射绿鬼一箭，谁不听话最后那一箭射谁！",
      "emotion": "happy",
      "image": "assets/emojis/666.png",
      "sfx": {"effect_id": "", "title": "恍然大悟"}
    }
  ]
}
```

字段说明（全部可选，缺省时回落到规则引擎）：
- `emotion`: `sad` / `happy` / `surprise` / `angry` / `neutral` — 影响贴纸与音效匹配
- `sfx`: `{"effect_id": "", "title": "中文描述"}` — 留空则按情绪自动匹配；若你已知剪映素材ID（如 `11089718624`）则填 `effect_id` 强制使用
- `image`: 贴纸图片路径（本地存在则直接使用，不再自动匹配）
- `is_narration`: `true` 强制按旁白样式渲染

完整示例见 [samples/emperor_wisdom_model.json](samples/emperor_wisdom_model.json)。

## 高级用法（JSON剧本模式）

如果你需要更精细控制，可以手动写JSON剧本（参考 `samples/` 目录），然后用原入口：

```bash
# JSON剧本模式（支持更多精细控制）
python scripts/gospel_automator.py samples/emperor_wisdom.json
python scripts/gospel_automator.py samples/emperor_wisdom.json --plan-only
python scripts/gospel_automator.py samples/emperor_wisdom.json --speaker zh_male_huoli
```

JSON剧本格式支持多角色音色映射：
```json
{
  "title": "视频标题",
  "bgm_query": "搞笑 悬疑",
  "role_speakers": {
    "年轻人": "BV056_streaming",
    "大师": "BV701_streaming",
    "旁白": "zh_male_voplvyou"
  },
  "messages": [
    {"role": "年轻人", "text": "...", "emotion": "sad"},
    {"role": "旁白", "text": "一分钟后", "is_narration": true}
  ]
}
```

### 常用音色ID

| 角色类型 | 推荐speaker_id |
|---|---|
| 年轻阳光男生 | `BV056_streaming` |
| 中年沉稳男声 | `BV701_streaming` |
| 播音旁白 | `zh_male_voplvyou` |
| 雅痞大叔 | `BV107_streaming` |
| 磁性男声 | `zh_male_iclvop_zhangjinxiangnanzhu` |
| 活力吐槽 | `zh_male_huoli`（默认） |
| 新闻播报 | `BV002_streaming` |

## 示例

项目自带示例文件：
- [samples/emperor_wisdom.txt](samples/emperor_wisdom.txt) — "帝王之术"对话（含旁白+音色建议）
- [samples/demo_apology.json](samples/demo_apology.json) — 中介道歉对话

运行示例：
```bash
# 一键生成"帝王之术"视频
python scripts/gospel_dialog.py -f samples/emperor_wisdom.txt -t "帝王之术"
```

## 执行环境注意事项（Windows / bash）

在 AI 助手（Codex/OpenCode 等）中运行本流水线时注意：

- **聊天视频 webm 预览需要 Playwright**：`chat_scene_renderer` 渲染微信聊天界面为 webm 依赖 `playwright` + Chromium（`pip install playwright && playwright install chromium`）。**缺失时仅影响 webm 预览，不影响剪映草稿组装**——若出现「未安装 playwright」报错，草稿仍会正常产出，可跳过预览直接去剪映看草稿。v6 新增的 QA 闸门/ASR/资产沉淀均不依赖 Playwright。

- **Windows 终端编码**：脚本输出中文日志，Windows 下需加 `PYTHONIOENCODING=utf-8` 前缀执行，否则报 `UnicodeEncodeError`（GBK）。
- **不要用多行 `python -c`**：win10 git-bash 中带中文的多行 `python -c "..."` 偶发 `No such file or directory`（引号/编码问题）。稳妥做法：写临时 `.py` 文件到 `%TEMP%\opencode\` 再执行，或直接调用脚本入口。
- **中文文件名在 bash 中不可靠**：git-bash 中含中文字符的文件路径会导致 `No such file or directory`（即使文件存在）。解决方案：
  - 使用通配符 glob：`cp works/260802-01/plan* assets/plan.json`
  - 复制到 skill 内部纯 ASCII 路径：先 `cp` 到 `assets/plan.json` 再运行
  - **推荐**：plan.json 文件名始终用纯 ASCII（如 `plan.json`、`plan_dialog.json`），避免中文
- **素材路径解析规则**（`_resolve_asset_path`）：绝对路径直接用；相对路径按 plan.json 所在目录解析；仅文件名则在 skill 内置 `assets/emojis/`、`assets/sfx/` 兜底。素材放错位置会解析不到，落草稿时该段被跳过。
- **贴图素材宽高**：落草稿时脚本读取素材真实宽高计算 `scale`/`position`（calc_sticker_layout），`plan.json` 中写的 `scale`/`position` 仅为占位；`--plan-json` 运行时自动规范化写回 plan.json，保证 plan 与剪映草稿数值一致。

## 常见问题

- **对账闸门报了一堆 warning，能继续吗**：可以。warning 不阻断生成（默认容错），多为"素材缺失/旁白需手动配音/气泡重叠"等提示，建议生成后到剪映核对；error 级别（时间窗无效等）在 `--strict-qa` 下会建议中断。
- **想强制严格卡关，不修好不让生成**：加 `--strict-qa` 参数（在 `gospel_dialog.py` 透传，或在 `build_default_pipeline` 的 source 设 `strict_qa=True`）。
- **ASR 对齐报错 / 没装 faster-whisper**：流水线会自动降级到 VAD 静音检测（零额外依赖）。要启用 ASR：`pip install faster-whisper` 后重跑整曲模式即可享受毫秒级精度。
- **资产库 `assets/library/` 有什么用**：每次运行自动归档剧本结构化信息（角色/情绪/音乐推荐/素材命中/QA 结果），跨项目积累后可查可复用，印证"资产沉淀"——不必每次从零决策音乐风格与素材。
- **复刻模式生成的和参考视频一模一样吗**：不会。复刻的是**剧本结构/角色/情绪节奏**，配音与贴纸由本流水线重新生成，等于"用你的素材再拍一条同类型"，规避侵权且可换产品/换角色。
- **画面和歌曲对不上怎么办**：编辑 `assets/custom_audio/<name>/timings_<name>.json` 里的 `start_s`/`end_s`，然后加 `--timings <file>` 重跑
- **静音检测不准**：用 `--silence-db -20`（更严格，只检测真正的静音）或 `--silence-db -30`（更宽松，轻呼吸声也算停顿）
- **贴纸太大/太小/变形**：落草稿时按素材宽高等比缩放（缩放后宽=舞台宽1080），scale 上限 5.0（剪映建议 0.1~5.0，1.0=原始大小）；小图放大会钳制到 5.0 并居中显示，避免过度放大模糊。改素材本身（换高清图）比调参数更有效
- **音效太多/太少**：调整对话文本的情绪词和标点，或修改plan.json
- **旁白TTS和歌曲重叠**：在timings.json中调大旁白前后的间隙，或减小旁白音量
- **BGM找不到**：整曲模式下不需要BGM（整曲本身就是歌曲）；TTS模式下尝试更通用的关键词
- **自动导出失败**：剪映8.x不支持自动导出，在剪映中手动导出即可（草稿完整可用）

## AB 对话模式常见问题

| 问题 | 根因 | 解决方案 |
|------|------|----------|
| **音画不同步** | `--plan-json` 模式跳过 ASR，使用 JSON 手动时间戳 | 同时提供 `--dialog-json` + `--audio-mp3`，脚本自动做 ASR 对齐 |
| **头像没显示** | `fill_draft_from_plan` 把 speaker 规范化为 A/B，但 characters 用中文名作 key | 支持中文角色名自动映射（如「贾总」→A，「王工」→B） |
| **表情包没插入** | 自动模式规则引擎匹配失败，`--plan-json` 保留空 material | 用大模型语义决策填充 sticker 段的 material 后再跑 |
| **dialog.json 角色名不支持** | 脚本只接受 A/B 作为 role | 现已支持中文角色名（自动映射到 A/B） |

### 推荐工作流（确保一次成功）

```bash
# 1. 准备素材（dialog.json 用中文角色名也行）
# 2. 写 plan.json（大模型语义决策填充 sticker/audio 段）
# 3. 同时提供 plan-json + dialog-json + audio-mp3，脚本自动 ASR 对齐
python scripts/ab_generator.py \
  --plan-json plan.json \
  --dialog-json dialog.json \
  --audio-mp3 voice.mp3
```

**关键点**：
- `--plan-json` 提供语义决策（表情包/音效）
- `--dialog-json` + `--audio-mp3` 触发 ASR 时间戳对齐
- 三者结合 = 音画同步 + 头像显示 + 表情包插入
