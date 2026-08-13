# AB 双人对话短视频 · 剪映 5.9 草稿生成器 使用说明

> 适用场景：你已有一段**完整的双人配音音频**（人工录制 / 其他工具生成），以及一份 **AB 对话剧本**（JSON）。本工具用 faster-whisper ASR 自动识别音频中的每句话并精确对齐到剧本，然后直接生成剪映 5.9 可编辑草稿——**不需要 TTS、不切割音频、气泡不重叠**。

入口脚本：`scripts/ab_generator.py`（v5 新增，与福音流水线 `gospel_dialog.py` 相互独立，互不影响）

---

## 1. 环境准备（一次性）

- Python 3.9+（推荐 3.11+）
- 剪映专业版 5.9+（Windows/macOS 均可，输出为其可编辑草稿）
- Python 依赖（一次性安装）：
  ```bash
  pip install -r requirements.txt
  # faster-whisper 首次运行会自动下载 small 模型（约 460MB，需联网）
  ```
- `pyJianYingDraft`（剪映草稿生成库）：已随仓库分发于 `vendor/` 目录，脚本自动引入，**无需 pip install**。如使用自定义路径，可设置环境变量 `PYJYD_VENDOR` 指向其父目录。

检查依赖：

```bash
python -c "import faster_whisper; print('faster-whisper OK')"
```

> 若提示找不到 `faster_whisper`：`python -m pip install faster-whisper`

---

## 2. 准备输入（4 项必需）

### 2.1 对话剧本 JSON（`--dialog-json`）

纯 A/B 双人对话，**角色仅限 A、B**：

```json
[
  {"role": "A", "content": "大师，最近生意很不好，有什么方法可以改变吗？"},
  {"role": "B", "content": "问你一个问题"},
  {"role": "B", "content": "现在有两只鬼要吃掉你"},
  {"role": "A", "content": "先射绿鬼一箭，谁不听话最后那一箭射谁！"},
  {"role": "B", "content": "贫道佩服！"}
]
```

- 文件必须是 `.json` 后缀、UTF-8 编码
- 顺序即发言顺序，须与配音音频一致

### 2.2 完整配音 MP3（`--audio-mp3`）

- 一整段双人配音音频文件（`.mp3` 后缀）
- 系统会自动转写并逐句对齐时间戳，无需手工标注

### 2.3 贴图素材库目录（`--sticker-dir`，可选）

**缺省用 skill 内置贴图库 `assets/emojis/`**，想用自定义贴图时传此参数覆盖。

- 任意目录，系统**递归扫描** `.png/.jpg/.jpeg/.gif/.webp/.bmp`
- 推荐用情绪关键词命名文件，可提高自动匹配准确率：

| 情绪 | 规范文件名 |
|---|---|
| happy | `happy_01.png` 等（开心/高兴/哈哈/笑） |
| sad | `sad_01.png` 等（难过/伤心/哭/唉） |
| angry | `angry_01.png` 等（生气/卧槽/气死/烦） |
| surprise | `surprise_01.png` 等（惊讶/震惊/哇/天哪） |

- 素材**本地优先**：文件名命中情绪词优先选用；无匹配则不添加贴图（不强制）
- 内置示例库：`assets/emojis/`

### 2.4 音效素材库目录（`--sound-dir`，可选）

**缺省用 skill 内置音效库 `assets/sfx/`**，想用自定义音效时传此参数覆盖。

- 任意目录，递归扫描 `.mp3/.wav/.m4a/.aac/.ogg/.flac`
- 系统在**强情绪/感叹句**（如"卧槽""哈哈""！""？"）处选择音效，单条最长 1.5 秒、相邻不重叠
- 无匹配则不添加音效（不强制）
- 内置示例库：`assets/sfx/`

---

## 3. 内置剪映 5.9 空白草稿模板（`--template-dir`，可选）

**无需自己准备模板，也无需传此参数**——缺省用 skill 内置模板：

```
assets/blank_template/     ← 直接填这个目录（含 draft_info.json 等完整模板）
```

- 系统会以该模板为基础创建新草稿，**模板本身不会被污染**（每次复制后填充）
- 若你有自己的剪映 5.9 空白草稿（含 `draft_content.json` 或 `draft_info.json` 或 `draft_meta_info.json`），也可指向它

---

## 4. 运行

> **推荐：由 AI 助手（大模型）按 skill 工作流执行**——本工具定位是「大模型语义决策 + 脚本机械执行」的结合。不要在对话中把下面的裸命令直接丢给用户跑，那会走纯规则引擎、跳过大模型对表情包/音效的语义判断（匹配质量明显下降）。正确流程（详见 `SKILL.md` 的 AB 双人对话模式章节）：
>
> 1. 大模型读取 `dialog.json` 与 `assets/emoji_scenes.md`（161 张表情包场景库：核心情绪/标签/画面描述）与 `assets/sfx/`
> 2. 大模型**逐条按台词语义决策**：哪条消息需要表情包（从 emoji_scenes.md 挑画面最贴合的一张，`material` 填文件名）、哪条需要音效（平淡陈述句不配）
> 3. 大模型把决策写入中间 JSON（见 4.2 与第 8 节 schema），`material` 填文件名即可（脚本自动在 `assets/emojis`/`assets/sfx` 下解析）
> 4. 调用 `--plan-json` 机械执行生成草稿

### 4.1 中间 JSON 驱动模式（`--plan-json`，推荐入口）

大模型完成语义决策后，把决策写入**中间 JSON**（遵循公开 schema，见第 8 节），再用它直接生成草稿，**跳过 ASR、秒出**：

```bash
# 用中间 JSON 直接生成草稿（无需 --dialog-json/--audio-mp3）
python scripts/ab_generator.py --plan-json plan.json
```

> 中间 JSON 中 `sticker`/`audio(sfx)` 段的 `material` 填文件名即可；脚本自动在 skill 内置素材库定位。`scale`/`position` 字段不参与落草稿计算（见第 7 节硬约束），由脚本统一重算，并在 `--plan-json` 运行时自动写回 plan.json 便于核对。

### 4.2 自动模式（`--dialog-json`，无模型介入时的兜底）

自动模式在生成草稿前会先产出一份**中间 JSON**（`plan_<草稿名>.json`，默认写在草稿同目录），语义分析由脚本内置规则引擎完成（准确率有限，仅作为无大模型介入时的兜底）：

```bash
python scripts/ab_generator.py \
    --dialog-json dialog.json \
    --audio-mp3 voice.mp3
```

> 必填参数仅 `--dialog-json` 与 `--audio-mp3`。其余参数均有 skill 内置缺省，一般无需传：
> - `--sticker-dir`：贴图素材库，缺省用 skill 内置 `assets/emojis`（可覆盖为自定义贴图目录）
> - `--sound-dir`：音效素材库，缺省用 skill 内置 `assets/sfx`
> - `--template-dir`：剪映 5.9 空白草稿模板，缺省用 skill 内置 `assets/blank_template`
> - `--output-draft`：草稿输出目录，缺省输出到**剪映默认草稿目录**（Windows：`%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft`，macOS：`~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft`），打开剪映即可在草稿箱看到。

也支持按固定顺序传位置参数（仅前 2 个生效：dialog.json、voice.mp3，其余一律用内置缺省）：

```bash
python scripts/ab_generator.py dialog.json voice.mp3
```

三种入口的优先级：**`--plan-json` > `--dialog-json` 自动模式**。给了 `--plan-json` 就以它为准；只给 `--export-plan` 而无 `--dialog-json` 时明确报错。

### 4.3 前置校验（8 条，不满足即中止并提示）

| # | 检查项 | 不通过提示 |
|---|---|---|
| 1 | 必填参数齐全（仅 `--dialog-json`、`--audio-mp3`，其余用内置缺省） | 缺少必要输入参数… |
| 2 | 对话 JSON 存在且为 .json | 对话文本路径无效… |
| 3 | MP3 存在且为 .mp3 | 配音音频文件不存在… |
| 4 | 贴图目录存在 | 贴图素材库目录不存在 |
| 5 | 音效目录存在 | 音效素材库目录不存在 |
| 6 | 模板目录完整 | 剪映5.9空白草稿模板不完整… |
| 7 | 输出目录可创建 | 输出目录无法创建… |
| 8 | 对话结构合法（A/B） | 对话文本格式异常… |

### 4.4 执行过程

```
正在执行前置校验…
正在扫描素材库…
正在进行语音识别(ASR)对齐, 请稍候...   ← 首次约 30–60 秒（加载模型+转写）
（ASR 逐句对齐 → 编导入场动画/贴图/音效 → 时间轴适配 → 模板填充）
执行完成！剪映草稿输出路径：<output_draft_dir>
草稿目录：<output_draft_dir>/ab_dialog_<时间戳>/
```

---

## 5. 输出

```
<output_draft_dir>/
└── ab_dialog_YYYYMMDD_HHMMSS/
    ├── draft_info.json        ← 剪映草稿主文件（在剪映中打开）
    ├── draft_meta_info.json
    ├── draft_settings/
    ├── key_value.json
    └── plan_ab_dialog_*.json  ← 自动模式导出的中间 JSON（可编辑后用 --plan-json 重跑）
```

用剪映（**专业版 5.9 或兼容版本**）打开 `draft_info.json` 所在目录即可看到草稿。轨道结构：

| 轨道 | 内容 |
|---|---|
| 视频轨（背景） | 空占位轨 |
| 音频轨 | 完整配音 MP3（整段，不切割） |
| 视频轨（贴图） | 情绪贴图（全宽贴底，无匹配则空） |
| 文本轨（气泡） | A 左 / B 右，黑描边白字，`displayStart = audio_start`，`displayEnd = audio_end + 0.4s`，重叠自动顺延 |
| 音效轨 | 强情绪点音效（≤1.5s，相邻不重叠） |

---

## 6. 常见问题

| 问题 | 处理 |
|---|---|
| ASR 对齐不准确（气泡时间与语音对不上） | 文本与音频内容必须严格一致；尽量减少同音/相似句；调整语速后再跑 |
| 首次运行很久 | 正在下载/加载 `small` 模型，之后走缓存即快 |
| 贴图/音效没加上 | 文件名需含情绪关键词（见 2.3/2.4），或素材目录里没有匹配文件；属于正常现象 |
| 模板校验不通过 | 确认 `--template-dir`（缺省为 skill 内置 `assets/blank_template`）指向含 `draft_content.json`/`draft_info.json`/`draft_meta_info.json` 的目录 |
| 剪映打不开草稿 | 确认剪映版本兼容 5.9 草稿格式；用"剪映 → 开始创作 → 导入草稿"方式打开 |

---

## 7. 与福音流水线的关系

| | AB 双人对话模式 | 福音吐槽流水线 |
|---|---|---|
| 入口 | `scripts/ab_generator.py` | `scripts/gospel_dialog.py` |
| 输入 | AB JSON + 完整配音 MP3 | 对话文本 / Suno 歌曲 |
| 配音 | 无需（自带音频） | auto TTS / manual / suno |
| 对齐 | faster-whisper ASR | VAD 静音检测 |
| 输出 | 剪映 5.9 草稿 | 剪映草稿 + 渲染视频 |

两者共用 `assets/emojis`、`assets/sfx` 素材库，可并行使用、互不影响。

---

## 8. 中间 JSON 驱动模式（完整说明）

自动模式在生成剪映草稿前，会先产出一份**中间 JSON**（`plan_<草稿名>.json`，默认写入草稿同目录）。这份 JSON 遵循下述公开 schema，三个用途：

1. **可读产物**：语义分析结果（每句台词的贴图/音效/动画选择）一目了然；
2. **可编辑输入**：修改后通过 `--plan-json` 直接生成草稿，**跳过 ASR、秒出**；
3. **可编程接口**：其他工具/大模型可生成此 JSON 驱动草稿。

### 8.1 两种模式与命令

| 模式 | 命令 | 说明 |
|---|---|---|
| **plan 模式（推荐）** | `python scripts/ab_generator.py --plan-json plan.json` | **大模型按台词语义决策表情包/音效后写入中间 JSON，脚本机械执行生成草稿**（无需 dialog/audio 参数，跳过 ASR，秒出） |
| 自动模式（兜底） | `python scripts/ab_generator.py --dialog-json dialog.json --audio-mp3 voice.mp3` | ASR 对齐 → **规则引擎**语义分析（准确率有限，仅无大模型介入时兜底）→ 生成草稿 + 草稿同目录导出 `plan_<name>.json` |
| 自动模式+额外导出 | 上面命令加 `--export-plan path.json` | 把中间 JSON 额外复制到指定路径 |

优先级：`--plan-json` > 自动模式。仅给 `--export-plan` 而无 `--dialog-json` 时，报错提示必须走自动模式或 plan 模式。

> **为什么推荐 plan 模式**：表情包/音效是否添加、选哪张，应由大模型根据台词语义分析决定（参考 skill 根目录 `assets/emoji_scenes.md`，161 张表情包场景库，含核心情绪/标签/画面描述），而不是交给脚本关键词规则。大模型把决策写入中间 JSON 的 `sticker`/`audio(sfx)` 段 `material`（填文件名即可），脚本负责素材定位、ASR 对齐与剪映草稿填充。

### 8.2 schema 字段说明

```json
{
  "meta": {
    "title": "视频标题", "resolution": [1080, 1920], "fps": 30,
    "aspect_ratio": "9:16", "bg_color": "#000000", "chat_bg_color": "#f5f5f5",
    "output": "output/final_video.mp4", "render_quality": "high"
  },
  "characters": {
    "A": {"name": "老业主 王哥", "avatar": "assets/avatars/wangge.jpg", "avatar_size": [60,60],
          "avatar_border": "circle", "bubble_color": "#ffffff", "bubble_shadow": "soft",
          "bubble_border_radius": 18, "text_color": "#000000", "text_font": "PingFangSC-Regular",
          "text_font_size": 32, "side": "left", "position": {"x":50, "y":50}},
    "B": {"name": "...", "bubble_color": "#95ec69", "side": "right"}
  },
  "tracks": [
    {"type": "video",   "segments": [{"startTime": 0, "endTime": 39, "material": "背景图路径", "opacity": 1.0}]},
    {"type": "audio",   "segments": [{"startTime": 0, "endTime": 39, "material": "bgm.mp3", "volume": 0.15, "fade_in": 0.5, "fade_out": 0.5}]},
    {"type": "text",    "segments": [{"startTime": 1.0, "endTime": 2.5, "material": "曾小黑在吗？", "speaker": "A",
                                      "style": {"font": "PingFangSC-Regular", "size": 32, "color": "#000000",
                                                "align": "left", "line_height": 1.5},
                                      "animation": {"type": "pop_in", "duration": 0.3, "easing": "ease-out", "delay": 0},
                                      "position": {"x": 50, "y": 50, "anchor": "left"}}]},
    {"type": "sticker", "segments": [{"startTime": 1.5, "endTime": 2.1, "material": "贴图路径或空", "scale": 0.5,
                                      "animation": {"type": "pop_in", "duration": 0.3},
                                      "position": {"x": "50", "y": "30", "anchor": "center"}, "hint": "提示词"}]},
    // 注：sticker 段 scale/position 仅为占位（可省略），落草稿时由脚本按素材真实宽高重算并写回 plan.json
    {"type": "audio",   "segments": [{"startTime": 1.0, "endTime": 2.5, "material": "音效.mp3", "volume": 0.5}]}
  ],
  "transitions": [{"type": "dissolve", "duration": 0.3, "easing": "ease-in-out"}]
}
```

**各字段含义**：

| 字段 | 说明 |
|---|---|
| `meta.title` | 视频标题（当前脚本固定写入，未用于重命名草稿） |
| `meta.resolution` | 画布尺寸，固定 `[1080, 1920]` |
| `meta.fps` | 帧率，固定 30 |
| `characters.A/B` | 角色配置：`side`=left/right（气泡贴边方向）、`bubble_color`=气泡底色（A 默认白、B 默认微信绿 `#95ec69`）、`text_color`/`avatar`（A.png/B.png） |
| `tracks[].type` | `video`（背景图）/ `audio`（配音+BGM+音效）/ `text`（气泡）/ `sticker`（贴图） |
| `text.segments[].speaker` | **必填** A/B，决定气泡贴左/右与布局定位 |
| `text.segments[].material` | 该条气泡的台词文本 |
| `sticker.segments[].material` | 贴图路径；**为空时表示规则未匹配**，`hint` 字段给出建议（见 8.3） |
| `sticker.segments[].hint` | 提示词（建议补什么贴图/音效） |
| `audio.segments[].volume/fade_in/fade_out` | 音量与淡入淡出（秒） |

### 8.3 hint 提示词机制

语义分析（规则引擎）对每句台词匹配贴图/音效，**匹配不到时不中断流程**：

- 匹配成功 → 段 `material` 填素材路径；
- 匹配失败 → 段 `material` 为空、`hint` 填建议（如 `"建议：happy 类贴图（assets/emojis 下）"`），运行日志同时打印中文提示（如 `提示：第N条消息未匹配贴图，hint=...`）。

你可以按 hint 补素材后改 JSON 重跑，或让大模型读取 hint 自动补全素材路径。

### 8.4 硬约束（中间 JSON 中的值不得覆盖）

以下两项由脚本强制计算，**JSON 里的对应字段仅供参考**：

1. **音画同步**：气泡/贴图显示时间由 ASR 对齐的 `audio_start`/`audio_end` 决定（`display_start=audio_start`、`display_end=audio_end+0.4s`，相邻消息重叠自动顺延）。text/sticker 段的 `startTime`/`endTime` 在落草稿时按此规则重算。
2. **贴图尺寸位置**（calc_sticker_layout，全宽贴底）：等比缩放使缩放后宽度 = 舞台宽 1080px（scale = 1080/素材宽）；**scale 上限 5.0**（剪映贴纸缩放建议范围 0.1~5.0，1.0=原始大小；小图若 1080/宽 > 5.0 则钳制为 5.0，水平居中显示、不再铺满全宽，防过度放大模糊变形）；超高保护——原始高 × scale > 画布高 95%（1824px）时按可用高度回缩，保证完整显示不溢出；水平居中（transform_x = 0）、垂直贴底（贴图底边距画布底 5% 舞台高），与气泡位置无关。JSON 中 `scale`/`position` 不参与计算，`--plan-json` 运行时脚本自动读取素材真实宽高重算并**写回 plan.json**（保证 plan 与草稿数值一致，`material` 写回原名不落绝对路径）。**坐标系注意**：JianYing 的 transform.y 归一化到画布高(1920)，transform.x 归一化到半画布宽(540)——这是 JianYing 的不一致行为，pyJianYingDraft 文档说"半个画布高"但实际 UI 验证是画布高

### 8.5 音频轨约定

- **完整配音**：`audio` 轨第一段 `material` 若等于配音音频路径（自动模式下即 `--audio-mp3`），整曲铺满 0 到总时长；
- **BGM/音效**：其余 `audio` 段按各自 `startTime`/`endTime` 放置，支持 `volume`、`fade_in`/`fade_out`（淡入淡出），重叠自动顺延；与配音同文件的段会跳过（避免重复铺整曲）。

### 8.6 常见问题

| 问题 | 处理 |
|---|---|
| 怎么改气泡颜色/文字？ | 编辑 `characters` 里对应角色的 `bubble_color`/`text_color`，`--plan-json` 重跑 |
| 怎么换头像？ | `characters.A.avatar`/`characters.B.avatar` 填图片路径（方形会被圆形裁剪），或自动模式下在 dialog.json 同目录放 `A.png`/`B.png` |
| 贴图没配上/想换 | 看该段 `hint`，按建议补素材路径到 `material`，重跑 |
| 想让某句不显示气泡 | 删除对应 text 段即可 |
| 想加背景图 | `video` 轨加一段 `material` 指向图片（自动模式同目录 `BG.png` 自动铺满） |
| plan JSON 修改后直接重跑会重新 ASR 吗 | 不会，`--plan-json` 模式跳过 ASR，秒出草稿 |
