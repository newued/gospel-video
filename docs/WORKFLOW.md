# 福音吐槽视频自动流水线 — 工作流方案文档

> 版本：v3（融合连续渲染 + plan.json 中间产物 + 贴纸双轨定位 + 整体倍速）
> 日期：2026-07-31
> 项目根：`D:/wzk/fuyin-video/shengyibuhao`

## 1. 目标

把"福音吐槽风格视频"的制作自动化：**微信聊天界面 + 消息逐条弹出 + 表情包/图片插入 + 根据对话内容随机音效/转场/字幕样式**，输出可直接在剪映中二次编辑的草稿，或一键导出成片。

人工传统流程（聊天模拟器逐条截屏 → 福音风格配音 → 剪映调动效/贴纸/音效 → 导出）被拆解为可重复执行的流水线。

## 2. 架构总览

流水线融合两个方案的优点：

| 方案 | 核心思路 | 采用的优点 |
|------|---------|-----------|
| A：连续渲染 | HTML+playwright 一次录屏出聊天视频，时间轴原生 | 单一视频素材进剪映、CSS 动画丰富、转场/字幕动画原生 |
| B：帧序列+plan | render_frames 逐帧截图 + plan.json 显式中间产物 | 渲染与编排解耦、plan.json 可人工审核/修改、素材映射不猜 ID |

**三阶段流水线**：

```
┌────────────┐   阶段1    ┌──────────────┐   阶段2    ┌──────────────┐   阶段3    ┌────────────┐
│ 剧本 JSON   │ ────────> │  build_plan   │ ────────> │ 聊天视频渲染   │ ────────> │ 剪映草稿组装  │ ──> 导出 mp4
│ dialogue.json│  配音+时序 │  plan.json    │  录屏webm  │ (playwright) │ 按plan映射  │ (JyProject) │
└────────────┘  +素材匹配  └──────────────┘            └──────────────┘            └────────────┘
                    │
                    ▼
         人工审核/修改 plan.json
         （补素材 ID、换贴纸路径）
```

**关键设计：plan.json 是核心中间产物**。生成与编排解耦——先产出完整计划（每条消息的时序/配音/音效映射/贴纸/估算），人工审核补素材后再按计划组装。这解决了两类问题：

1. **素材匹配不确定**：音效/贴纸自动匹配可能失败，plan.json 显式列出 missing 清单，人工去剪映素材库找到后填回 ID 再跑，不猜、不凑合。
2. **流程可中断**：`--plan-only` 只跑到阶段2，适合先渲染看效果、人工介入后再编排。

## 3. 目录结构

```
shengyibuhao/
├── scripts/
│   ├── gospel_automator.py        # 主流水线（三阶段编排 + CLI）
│   ├── chat_scene_renderer.py     # 阶段2：微信聊天场景 HTML 渲染 + 录屏
│   ├── random_assets.py           # 素材随机器（读 skill 的 data/*.csv）
│   ├── make_placeholder_emojis.py # 工具：生成占位表情包 PNG
│   └── examples/
│       └── dialogue.json          # 示例剧本（8 条消息）
├── assets/
│   ├── avatars/                   # 头像 PNG（150x150，可选）
│   ├── emojis/                    # 表情包贴纸库（含用户积累的真实表情，png/jpg/gif/webp）
│   ├── emoji_scenes.json/.md      # 表情包语义索引（filename/tags/emotion，匹配优先）
│   ├── tts/                       # 配音产物 voice_NNN.mp3（--speed 变速另存 *_x1.3.mp3）
│   ├── chat_video/                # 聊天视频 webm（自动生成）
│   └── plan.json                  # plan 中间产物（自动生成，可人工改）
└── docs/
    ├── WORKFLOW.md                # 本文档
    └── USER_GUIDE.md              # 用户使用文档
```

## 4. 数据流详解

### 4.1 剧本 JSON（输入）

```json
{
  "title": "福音吐槽_示例",
  "project_name": "GospelDemo_01",
  "resolution": "1080",
  "speaker": "zh_male_huoli",
  "bgm_query": "7546546694282676275",
  "messages": [
    {"role": "张三", "type": "text", "text": "这波操作我直接血压拉满", "emotion": "angry"},
    {"role": "李四", "type": "sticker", "image": "assets/emojis/angry_01.png", "emotion": "angry"},
    {"role": "张三", "type": "text", "text": "哈哈哈绝了", "emotion": "happy", "sticker": "assets/emojis/happy_01.png"}
  ]
}
```

字段说明：
- `resolution`: `"1080"`（竖屏 1080x1920）| `"1920"`（横屏 1920x1080）
- `speaker`: TTS 音色 ID（默认 zh_male_huoli），见 skill 的 `data/tts_speakers.csv`
- `bgm_query`: BGM 素材 ID（可留空）
- 消息字段：
  - `role`: 发送者昵称（影响气泡左右侧）
  - `type`: `text` 文字气泡 | `sticker` 表情包/图片消息
  - `text`: 文字内容（sticker 消息可省略）
  - `image`: sticker 消息的图片路径
  - `sticker`: 可选，text 消息显式声明的贴纸路径（= 正式对话内容，进气泡渲染，缺失必须提示；不声明则自动按情绪匹配覆盖贴纸）
  - `emotion`: `angry|happy|sad|surprise|neutral`，驱动音效/贴纸/随机匹配
  - `sfx`: 可选，指定音效 effect_id（不指定则按 emotion 自动匹配）

### 4.2 plan.json（中间产物，schema v3）

```json
{
  "schema_version": 3,
  "title": "...", "project_name": "...", "resolution": "1080",
  "speaker": "zh_male_huoli", "bgm_query": null,
  "messages": [{
    "index": 0, "role": "张三", "type": "text", "text": "...",
    "emotion": "angry", "start_s": 0.5, "end_s": 3.1, "duration_s": 2.6,
    "voice": "D:/.../assets/tts/voice_000.mp3",
    "sfx": {"effect_id": "760585...", "title": "爆炸声效19", "source": "auto"},
    "sticker_path": null,
    "overlay_sticker": "D:/.../assets/emojis/angry_01.png"
  }],
  "missing_assets": [{"type": "sfx|sticker", "index": 2, "emotion": "sad",
                       "detail": "...", "hint": "..."}],
  "estimates": {"total_duration_s": 25.7, "message_count": 8, "voice_count": 7,
                "sfx_count": 7, "sticker_count": 1, "overlay_count": 7,
                "missing_count": 0,
                "asset_points_total": 15, "asset_points_used": 15}
}
```

- `messages[i].start_s/end_s`：消息在时间轴上的绝对位置（秒），渲染与配音/字幕/音效共用，保证对齐
- `messages[i].voice`：该消息配音文件路径（sticker 消息为 null）
- `messages[i].sfx`：音效映射。`source: "auto"` 为自动匹配，`"manual"` 为剧本指定或人工填写
- `messages[i].sticker_path`：**正式对话内容**贴纸（sticker 消息 / 剧本显式声明的贴纸），渲染在聊天气泡内；null 表示无
- `messages[i].overlay_sticker`：**覆盖贴纸**（普通 text 消息的情绪补充，非正式对话内容），在剪映层画中画叠加，突出喜剧节奏；null 表示未匹配到
- `missing_assets`：不凑合清单（见 §6）
- `estimates`：积分估算——成片时长、素材使用量、缺失数（预算参考）

**人工修改约定**：把剪映素材库中找到的音效 ID 填进 `messages[i].sfx.effect_id`（source 改 `"manual"`），把真实贴纸路径填进 `messages[i].sticker_path`（正式内容）或 `messages[i].overlay_sticker`（覆盖贴纸），然后 `--plan <path>` 重跑即采用人工值。

### 4.3 时间轴对齐模型

```
0s     0.5s     3.1s      5.1s     ...
│      ├msg0───┤ │ ├msg1──┤ ...
│      配音0    │  │ 配音1  │
│      字幕0    │  │ 字幕1  │
│      音效0    │  │ 音效1  │
├──────────────────────────────┤ 聊天视频（总长 = 最后end + 1.5s 结尾缓冲）
```

- 每条消息时长 = 配音实测时长（ffprobe），sticker 消息固定 1.2s，无配音文本兜底 1.5s
- 消息间留白 `MSG_GAP_S = 0.4s`（换人时完整留白，逐条分明）；**同一个人连续说话时留白减半（0.2s，气泡紧凑连续）**，避免冷场；首条偏移 `FIRST_MSG_OFFSET_S = 0.5s`
- 聊天视频 HTML 用 CSS `animation-delay` 精确对齐每条消息的弹出时刻（第 N 条消息在 start_s 弹出）——等效于"第 N 帧 = 前 N 条消息"

### 4.4 素材匹配（阶段1）

| 素材 | 数据源 | 匹配策略 | 缺失行为 |
|------|--------|---------|---------|
| 音效 | skill `data/cloud_sound_effects.csv` | **智能评估**：仅强情绪（angry/surprise）/含语气词（！？卧槽哈哈等）/包袱句（结尾语气词长句）才配；平淡消息静默不配；同人连续时上一条已配则本条跳过，避免音效轰炸 | 记 missing，不插入（不凑合） |
| 贴纸 | 项目 `assets/emojis/` + `emoji_scenes.json` 索引 | 先按索引（tags/emotion 语义）匹配，回退文件名关键词 | 正式内容记 missing；覆盖贴纸静默跳过 |
| 转场 | skill `data/transitions.csv` | 随机 identifier | 容错跳过 |
| 字幕动画 | skill `data/text_animations.csv` | 随机 identifier（每条消息随机不同动画，避免呆板） | 容错跳过 |
| BGM | skill 云素材（bgm_query） | 指定 ID | 容错跳过 |

## 5. 模块职责与接口

### 5.1 `scripts/gospel_automator.py` — 主流水线（CLI 入口）

```
python scripts/gospel_automator.py 剧本.json                 # 全流程
python scripts/gospel_automator.py 剧本.json --plan my.json  # 复用人工 plan
python scripts/gospel_automator.py 剧本.json --plan-only     # 只到阶段2
python scripts/gospel_automator.py 剧本.json --export out.mp4  # 额外导出
python scripts/gospel_automator.py 剧本.json --speaker X --draft-root PATH
python scripts/gospel_automator.py 剧本.json --speed 1.3     # 整体节奏倍速（配音变速+时序压缩）
```

`--speed`：整体节奏倍速（默认 1.3），`>1` 更快。配音经 ffmpeg atempo 变速，消息时序、字幕/音效/贴纸时间点同步压缩，聊天视频渲染时长同步缩放。复用 plan 时若 plan 的倍速与当前 `--speed` 不一致会告警（音视频不同步风险）。

核心函数：

```python
def build_plan(script: dict, speaker: str, plan_path: str = None,
               speedup: float = DEFAULT_SPEEDUP) -> dict
    # 阶段1：TTS 配音 + 时序计算 + 素材匹配（不凑合） + 积分估算，落盘 plan.json

def _assemble_draft(plan: dict, draft_root=None, export_video=None) -> dict
    # 阶段2+3：渲染聊天视频 + 按 plan 组装剪映草稿

def build_gospel_video(script_path, plan_path=None, draft_root=None,
                       export_video=None, force_speaker=None,
                       plan_only=False, speedup=DEFAULT_SPEEDUP) -> dict
    # 完整流水线入口；plan 已存在则跳过配音/匹配，用人工改过的映射
    # 返回 {ok, project_name, draft_path, chat_video, errors[], missing_assets[]}
```

`DEFAULT_SPEEDUP = 1.3`：整体节奏倍速，贯穿三阶段（build_plan 时序 / render 动画 / 组装时间点）。

### 5.2 `scripts/chat_scene_renderer.py` — 聊天场景渲染

```python
def build_chat_html(script: dict, msg_timings: list = None, speedup: float = 1.0, ...) -> str
    # 生成微信风格 HTML 字符串（灰底/标题栏/左右气泡/头像/时间戳/贴纸动画）
    # speedup: 整体倍速，缩放所有动画 duration/delay

def render_chat_scene(script: dict, output_video: str, width=1080, height=1920,
                      msg_timings: list = None, speedup: float = 1.0) -> bool
    # playwright 录屏输出 webm；window.animationFinished 标记结束
```

实现要点：
- 纯 CSS animation，不依赖网络；**每条消息/时间戳/贴纸从动画池随机取一种入场方式**（消息：popIn/slideUp/slideInLeft/slideInRight/zoomIn/fadeUp/bounceIn/swingIn/dropIn；时间戳：timeFade/timeSlide；贴纸：stickerBounce/stickerSwing/stickerZoom/stickerSpin/stickerDrop），相邻两条避免重复，杜绝呆板
- `animation-delay` 由 msg_timings 精确控制
- 消息过多时 zoom 缩放保证首屏全见
- Windows 控制台 UTF-8 重配置避免 emoji 崩溃

### 5.3 `scripts/random_assets.py` — 素材随机器

```python
random_transition() -> str          # 随机转场 identifier
random_text_animation() -> str      # 随机字幕动画 identifier
random_scene_effect() -> str        # 随机画面特效 identifier（保留扩展）
random_tts_speaker() -> str         # 随机音色 ID
sfx_for_emotion(emotion) -> dict|None   # 情绪→音效映射，失败返回 None
pick_sticker(emotion) -> str|None    # 情绪→贴纸路径，不匹配返回 None（不凑合）
```

## 6. 不凑合规则（重要设计约束）

需求约束：**匹配不到的音效/贴纸等素材，绝不随机凑合；必须备注提示，由人工去剪映素材库寻找插入**。

实现分层：

| 场景 | 行为 |
|------|------|
| 音效情绪匹配失败 | 记 missing_assets，不插入 |
| 音效下载失败（网络/ID 失效） | 记 missing_assets，不插入 |
| sticker 消息贴纸找不到 | 记 missing_assets，不插入（消息主体缺失，必须提示） |
| text 消息显式声明贴纸但文件不存在 | 记 missing_assets，不插入 |
| text 消息未声明贴纸（覆盖贴纸，可选增强） | 按情绪从本地表情库匹配（emoji_scenes.json 索引优先），匹配不到静默跳过（不算缺失，避免噪音） |
| 转场/字幕动画/BGM 失败 | 容错跳过（非情绪核心素材） |

`missing_assets` 在 plan.json 落盘 + 运行结束打印人工补素材清单（类型/消息序号/情绪/问题/建议）。

## 7. 集成要点（剪映草稿层）

基于 skill `jianying-editor` 的 `JyProject` 封装：

- 草稿根：`%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft`（Windows）/ `~/Movies/JianyingPro/...`（macOS）
- 竖屏项目：`JyProject(name, width=1080, height=1920)`
- 聊天视频：`add_media_safe(webm, "0s", duration, "VideoTrack")`（内部自动转 mp4）
- 配音：`add_audio_safe(mp3, start_time, duration, "VoiceTrack")`
- 字幕：`add_text_simple(text, start, dur, anim_in=..., clip_settings=draft.ClipSettings(transform_y=-0.82))`
- 贴纸双轨定位（用户规则）：
  - **正式内容贴纸**（sticker 消息 / 剧本显式 `image`/`sticker` 声明）→ `sticker_path`，渲染进聊天气泡内，缺失必须提示
  - **覆盖贴纸**（普通 text 消息的情绪补充）→ `overlay_sticker`，画中画叠加：`add_media_safe(png, ...)`，**位置从 5 个预置点随机（右上/右中/左上/左中/顶部），入场动画随机 4 种（pop 缩放弹出 / drop 掉落 / spin 旋转 / swing 回弹）**，突出喜剧节奏，非正式对话内容
- 音效：**`_match_sfx` 智能评估**——强情绪（angry/surprise）、文本含语气词（！？卧槽/哈哈/无语/绝了/麻了/离谱/破防/啊？/哦？/？？/啊/呢/吗/吧/唉/哟/哦）、包袱句（≥10 字且结尾语气词）→ 配；平淡消息静默不配；同一个人连续说话时上一条已配则本条跳过（避免音效轰炸）。匹配成功 `add_cloud_media(str(effect_id), start_time, "SFX_Track")`
- 转场：`add_transition_simple(名称, video_segment=chat_seg, duration="0.8s")`
- BGM：`add_cloud_music(query, "0s", total)` + `volume=0.4`
- 导出：`auto_exporter.auto_export(draft_name, out.mp4, resolution, framerate)`（需剪映开着）

## 8. 验证与测试

已通过实测验证（2026-07-31，示例剧本 8 条消息，默认 1.3× 倍速）：

1. **阶段1 输出**：plan.json（schema v3）正确落盘，missing_assets 清单准确
2. **阶段2 输出**：聊天视频 webm 渲染成功（约 40-50s）
3. **阶段3 输出**：草稿 `GospelDemo_01` 保存成功，draft_inspector 验证：
   - Tracks 7：VideoTrack/VoiceTrack/SFX_Track x3/StickerTrack/Subtitles
   - 20+ 段：配音 + 8 音效点 + 贴纸（正式内容 1 个进气泡、覆盖贴纸 7 个画中画缩放）+ 7 字幕（全部 transform.y=-0.82）
   - 转场 1 个、字幕动画 7 个
4. **贴纸双轨**：8 条消息中 7 条 text 消息全部匹配到本地表情包（emoji_scenes.json 索引优先）作为覆盖贴纸，1 条 sticker 消息作为正式内容进气泡，missing=0
5. **音效智能评估**：8 条消息仅 4 条配音效（疑问「然后呢？」提示音效、惊讶「？？？」爆炸、表情包爆炸、包袱句「一家人也得给加班费啊」搞笑音效），平淡消息静默——不再每条都炸
6. **入场随机化**：聊天视频中每条消息/时间戳/贴纸入场动画均从动画池随机（相邻不重复），覆盖贴纸位置与入场随机（5 位置 × 4 动画）
7. **容错**：纯标点文本（如「？？？」）TTS 失败为引擎固有行为（无字可读），无配音+有音效+字幕动画，行为合理

## 9. 扩展方向

- **帧序列模式**：若需剪辑软件内逐帧微调，可将 chat_scene_renderer 改为输出 PNG 序列 + plan.json（保留现有 webm 路径，二者等价）
- **多角色头像**：assets/avatars/ 放 150x150 透明 PNG，剧本 `avatar` 字段引用
- **新素材库**：向 skill `data/*.csv` 追加行（identifier/effect_id 需真实存在）
- **画面特效**：`random_scene_effect()` 已封装，按需在阶段3接入 `add_effect_simple`
- **BGM 自动选择**：按情绪从 cloud_music 库自动匹配
