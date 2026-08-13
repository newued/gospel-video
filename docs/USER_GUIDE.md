# 福音吐槽视频自动生成 — 用户使用文档

> 面向使用者：从零到成片，不需要会写代码。
> 配套：`WORKFLOW.md`（方案设计细节）

## 1. 快速开始（3 步跑通）

```bash
# 1. 用示例剧本跑全流程（自动生成配音、聊天视频、剪映草稿）
python scripts/gospel_automator.py scripts/examples/dialogue.json

# 2. 打开剪映，在草稿列表中查看「GospelDemo_01」

# 3. 导出成片（需剪映保持打开）
python scripts/gospel_automator.py scripts/examples/dialogue.json --export out.mp4
```

## 2. 环境要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10 |
| Python | 3.11+（已安装依赖：playwright / edge-tts / pymediainfo / requests 等） |
| 剪映 | v5.9+（导出功能依赖，需已登录并保持打开） |
| skill | `jianying-editor` 已安装（提供素材库与草稿生成能力） |

## 3. 完整工作流程

### 第 1 步：准备素材（可选）

**表情包**：放入 `assets/emojis/`（支持 png/jpg/gif/webp，文件名自带梗意即可）。可选同时维护 `assets/emoji_scenes.json`（每条含 `filename/filepath/tags/analysis` 语义索引），程序会优先用索引做标签匹配：

```
assets/emojis/
├── angry_01.png      ← 生气（愤怒红脸）
├── happy_01.png      ← 开心（大笑）
├── sad_01.png        ← 难过
└── surprise_01.png   ← 惊讶
```

> 没有素材也能跑：程序会自动按情绪从本地表情库匹配覆盖贴纸；正式内容贴纸（sticker 消息）匹配不到才会提示你补充（不会乱凑）。

**贴纸定位规则**：
- `type: "sticker"` 的消息或剧本显式声明的贴纸 = **正式对话内容**，渲染在聊天气泡内（缺失必须提示）
- 普通文字消息的表情包 = **覆盖贴纸**（情绪补充、突出喜剧节奏，非正式对话内容），自动按情绪从本地表情库匹配，以画中画形式覆盖在聊天上；**位置与入场动画随机**（右上/右中/左上/左中/顶部 × 弹出/掉落/旋转/回弹），每条都不一样，避免呆板

**音效规则**：**不是每条对话都配音效**。程序会智能评估：强情绪（生气/惊讶）、带语气词（！？卧槽/哈哈/无语/绝了/麻了/离谱/破防/啊？/哦？/啊/呢/吗/吧…）或包袱句（结尾带语气词的长句）才配；平淡消息静默不配；同一个人连续说话时上一条已配音效则本条跳过，避免音效轰炸。**如果你实在不知道配什么音效，可以不写，让程序评估**。

**头像**（可选）：放入 `assets/avatars/`，150x150 透明 PNG，剧本里用 `avatar` 字段引用。

### 第 2 步：写剧本

复制 `scripts/examples/dialogue.json` 改内容即可。最小字段：

```json
{
  "title": "我的第一个吐槽视频",
  "project_name": "MyVideo_01",
  "resolution": "1080",
  "speaker": "zh_male_huoli",
  "bgm_query": "7546546694282676275",
  "messages": [
    {"role": "张三", "type": "text", "text": "这波操作我直接血压拉满", "emotion": "angry"},
    {"role": "李四", "type": "sticker", "image": "assets/emojis/angry_01.png", "emotion": "angry"},
    {"role": "张三", "type": "text", "text": "哈哈哈绝了", "emotion": "happy"}
  ]
}
```

**字段速查**：

| 字段 | 必填 | 说明 |
|------|------|------|
| `title` | 是 | 视频标题（显示在聊天界面顶部） |
| `project_name` | 是 | 剪映草稿名（不能重复，重跑会自动覆盖同名） |
| `resolution` | 是 | `"1080"`=竖屏 9:16，`"1920"`=横屏 16:9 |
| `speaker` | 否 | 配音音色，默认 `zh_male_huoli`，可选 `zh_female_xiaopengyou` 等（见 skill 的 `data/tts_speakers.csv`） |
| `bgm_query` | 否 | 背景音乐素材 ID，留空则无 BGM |
| 消息 `role` | 是 | 昵称，决定气泡靠左/靠右 |
| 消息 `type` | 是 | `text`=文字气泡，`sticker`=表情包大图消息 |
| 消息 `text` | 条件 | `type=text` 必填；sticker 可省略 |
| 消息 `image` | 条件 | `type=sticker` 必填：图片文件路径 |
| 消息 `sticker` | 否 | 文字消息显式指定的贴纸路径（正式对话内容，进气泡；不填则自动按情绪匹配覆盖贴纸） |
| 消息 `emotion` | 否 | `angry/happy/sad/surprise/neutral`，自动匹配音效和贴纸 |

**写作技巧**（福音风格）：
- 消息多、节奏快、梗密度高
- 情绪变化丰富（生气→大笑→惊讶），触发不同音效
- 纯标点消息（如「？？？」）没有配音，但会配惊讶音效+字幕动画，制造喜剧停顿
- 结尾写包袱句（如「一家人也得给加班费啊」），会自动配搞笑音效收尾
- 消息入场动画/表情包出现方式会自动随机，无需干预

### 第 3 步：运行（三种模式）

```bash
# 模式 A：一条命令全流程（推荐，第一次跑这个）
python scripts/gospel_automator.py 我的剧本.json

# 模式 B：只生成计划和聊天视频，先看看效果（有素材缺失时推荐）
python scripts/gospel_automator.py 我的剧本.json --plan-only

# 模式 C：复用人工改过的计划（缺素材补完后用）
python scripts/gospel_automator.py 我的剧本.json --plan assets/plan.json

# 模式 D：控制节奏快慢（默认 1.3 倍速，数值越大越快）
python scripts/gospel_automator.py 我的剧本.json --speed 1.5
```

运行结束后会打印素材匹配报告：

```
✅ 素材全部匹配，无缺失          ← 完美，直接去剪映看
🔊 音效缺失: 消息2(sad) 未找到匹配音效，请到剪映素材库搜索并填入 plan.json
🖼️ 贴纸缺失: 消息5(angry) assets/emojis/angry_01.png 不存在
```

### 第 4 步：处理素材缺失（「不凑合」原则）

**如果报告了缺失，不要重跑碰运气**，按提示人工补：

1. 打开剪映 → 素材库 → 搜索匹配情绪的音效/贴纸
2. 打开 `assets/plan.json`
3. 找到对应消息条目（看 `index` 序号）：
   - 音效：把找到的音效 ID 填入 `sfx.effect_id`，`sfx.source` 改为 `"manual"`
   - 正式内容贴纸（sticker 消息）：把图片路径填入 `sticker_path`
   - 覆盖贴纸（可选增强，缺失会静默跳过）：把图片路径填入 `overlay_sticker`
4. 用模式 C 重跑

> 为什么不能凑合？匹配不到就随机换一个会破坏喜剧效果。缺失的宁可没有，也不能乱来。

### 第 5 步：剪映中微调与导出

- 剪映打开后刷新草稿列表，找到 `project_name` 对应的草稿
- 所有元素都是真实素材：配音、字幕、音效、贴纸、转场都可直接拖动修改
- 自动导出：`--export out.mp4`（需剪映开着，导出参数：1080p / 30fps）
- 手动导出：剪映右上角「导出」按钮

## 4. 常用命令速查

| 目的 | 命令 |
|------|------|
| 全流程生成 | `python scripts/gospel_automator.py 剧本.json` |
| 只看计划+聊天视频 | `python scripts/gospel_automator.py 剧本.json --plan-only` |
| 用改过的计划重跑 | `python scripts/gospel_automator.py 剧本.json --plan assets/plan.json` |
| 调整节奏倍速 | `... --speed 1.5`（默认 1.3，>1 更快） |
| 指定音色 | `... --speaker zh_female_xiaopengyou` |
| 指定剪映草稿目录 | `... --draft-root D:/my/drafts` |
| 生成后导出 mp4 | `... --export 输出.mp4` |
| 生成占位表情包 | `python scripts/make_placeholder_emojis.py` |

## 5. 输出产物说明

| 产物 | 位置 | 说明 |
|------|------|------|
| 配音 | `assets/tts/voice_NNN.mp3` | 每条消息一段 |
| 聊天视频 | `assets/chat_video/*.webm` | 微信界面动画（消息逐条弹出） |
| 计划文件 | `assets/plan.json` | 时序/素材映射，可人工修改 |
| 剪映草稿 | 剪映草稿根目录 | `%LOCALAPPDATA%\JianyingPro\User Data\Projects\com.lveditor.draft\<project_name>`（Windows）/ `~/Movies/JianyingPro/...`（macOS） |

## 6. 常见问题（FAQ）

**Q：运行报「剪映草稿已存在」？**
同名草稿会被覆盖（`overwrite=True`），换 `project_name` 或直接重跑即可。

**Q：配音少了一条？**
纯标点文本（`???`、`？？？`）没有可读文字，TTS 引擎无法配音——这是正常行为，该消息会以"音效+字幕动画"呈现，不需要处理。

**Q：音效下载失败（424）？**
网络或素材 ID 失效，按第 4 步人工补。

**Q：某条消息没配音效？**
正常。程序只对强情绪（生气/惊讶）、带语气词或包袱句配音效，平淡消息静默不配，避免音效轰炸。如果你希望某条必配音效，可在剧本里显式写 `"sfx": "音效ID"` 指定。

**Q：导出失败？**
确认剪映已打开并登录；确认剪映版本 ≥5.9。导出失败不影响草稿生成，可直接在剪映手动导出。

**Q：怎么换头像？**
把 150x150 PNG 放入 `assets/avatars/`，剧本消息加 `"avatar": "assets/avatars/我的头像.png"`。

**Q：视频太短/太长？**
消息停顿 0.4s（同一个人连续说话时减半为 0.2s，更紧凑），时长由配音决定。想加长就多写消息；想更紧凑用 `--speed`（如 `--speed 1.5` 整体加快）。

## 7. 注意事项

- 生成过程中会调用网络（TTS、音效下载），需要联网
- 聊天视频渲染约 30-60 秒，请耐心等待
- 表情包用真实表情替换占位图时，保持文件名含情绪关键词即可自动匹配
