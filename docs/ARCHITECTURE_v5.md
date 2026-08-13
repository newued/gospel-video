# 架构契约 v5（gospel-video 引擎化改造）

> 本文件是各模块实现的**契约**。所有模块只操作 `engine/models.py` 中的
> `Dialogue / Message / AudioClip / VisualSpec / Timeline / TimelineItem / Asset / PipelineContext`，
> 不互相传递零散变量或平行数组。

## 流水线（DAG）

```
文本/剧本
   │
   ▼
[ParserNode]       engine/parser.py       文本 -> List[RawMsg]（只解析结构）
   │
   ▼
[RoleMapNode]      engine/role_mapper.py  标签A/B/C -> 角色名 + 音色speaker_id
   │
   ▼
[EmotionNode]      engine/emotion.py      text -> emotion
   │
   ▼
[AudioPlannerNode] engine/audio_planner.py  Dialogue -> 逐句 audio（TTS/manual/song 三模式）
   │
   ▼
[AlignNode]        engine/alignment.py    整曲/逐句 -> 每句时间点（ASR优先/VAD/MANUAL）
   │
   ▼
[VisualPlannerNode] engine/visual_planner.py Dialogue -> 贴纸/音效/动画/转场（经 assets.py）
   │
   ▼
[TimelineNode]     engine/timeline_planner.py Dialogue -> Timeline（时间轴）
   │
   ▼
[QAGateNode] 🆕    engine/qa.py            Timeline -> 对账闸门（台词/配音/气泡/表情包/音效/连续性）
   │
   ▼
[RenderNode]       engine/renderer.py     Timeline -> HTML/视频（只执行，不决策）
   │
   ▼
[LibraryNode] 🆕   engine/library.py       归档剧本到 assets/library/（资产沉淀）
   │
   ▼
[AssembleNode]     gospel_automator.py     Timeline -> 剪映草稿
```

## 各模块契约

### engine/parser.py（纯解析）
```
@dataclass RawMsg: {speaker: str, text: str, narration: bool}
def parse_text(raw: str) -> List[RawMsg]
def parse_file(path: str) -> List[RawMsg]
```
只负责：`[A]xxx` / `A: xxx` / `大师：xxx` / 续行归属 / 音色建议行剥离。
**不做**：角色命名、情绪、音色推断。

### engine/role_mapper.py
```
def map_roles(raws: List[RawMsg], hints: dict) -> (List[Message], Dict[role, speaker_id])
```
- 角色名分配（年轻人/大师/师傅/老王...，旁白固定）
- 音色建议行解析（"A干净男生 B中年男人 旁白广播男音"）
- VOICE_KEYWORDS 关键词 -> speaker_id 映射

### engine/emotion.py
```
def detect_emotion(text: str) -> str   # angry|surprise|happy|sad|neutral
```
独立可替换（换情绪模型不动其他模块）。

### engine/alignment.py（Alignment Engine）
```
class AlignmentEngine:
    def __init__(self, mode="AUTO")   # AUTO|VAD|ASR|MANUAL
    def align(self, dialogue, audio_path=None, timings_path=None) -> List[(start_s, end_s)]
```
- VAD：能量曲线双阈值状态机（audio_aligner 的检测逻辑迁入）
- MANUAL：读 timings JSON
- ASR：预留（Whisper），未实现时 raise NotImplementedError
- AUTO：VAD 优先，失败降级静音检测

### engine/audio_planner.py（Audio Planner）
```
def plan_audio(dialogue, mode, custom_dir, tts_dir) -> Dialogue  # 回填 audio
```
- auto：逐句 TTS（走 CACHE），变速
- manual：整曲 find_full_song 或逐句映射 000.mp3；缺失回退 TTS
- suno：仅生成提示词（不在此节点）
- 复用 CACHE.get_tts/put_tts

### engine/assets.py（Asset Provider 插件化）
```
class AssetProvider(ABC):
    def find_sticker(self, emotion) -> Asset | None
    def find_sfx(self, emotion) -> Asset | None
    def find_transition(self) -> Asset
    def find_text_animation(self) -> Asset
    def find_avatar(self, role) -> Asset | None
    def find_bgm(self, query) -> Asset | None

class BuiltinProvider(AssetProvider):  # 封装原 random_assets.py
class Registry:  # 多 Provider 注册 + 顺序查询
def get_registry() -> Registry
```

### engine/visual_planner.py（Visual Planner）
```
def plan_visual(dialogue) -> Dialogue  # 回填 visual
```
- 贴纸/覆盖贴纸（走 AssetProvider）
- 音效克制规则（强喜剧/强情绪/同人连续跳过）
- 入场动画/转场/字幕动画（走 AssetProvider）
- 旁白不加贴纸

### engine/timeline_planner.py（plan -> Timeline）
```
def build_timeline(dialogue) -> Timeline
def save_timeline(timeline, path)
def load_timeline(path) -> Timeline
```
- message 轨：start/end/animation/side
- audio 轨：voice（逐句）+ song（整曲）+ bgm
- subtitle 轨：文本+文字动画
- effect 轨：贴纸/音效/转场

### engine/pipeline.py（DAG 编排）
```
class Node:  # 基类
    name: str
    def run(self, ctx: PipelineContext) -> PipelineContext

class Pipeline:
    nodes: List[Node]
    def add(self, node)
    def execute(self, ctx) -> PipelineContext   # 顺序执行 + HOOKS.run
    def rerun_from(self, node_name, ctx)         # 从某节点重跑
```
内置节点：ParserNode, RoleMapNode, EmotionNode, AudioPlannerNode,
AlignNode, VisualPlannerNode, TimelineNode, QAGateNode, RenderNode,
LibraryNode, AssembleNode。

### engine/renderer.py（Renderer 只执行）
```
def render_chat(timeline, dialogue, out_video, width=1080, height=1920) -> str
```
- 输入 Timeline，输出 HTML/视频
- **不做**动画决策（动画名已在 timeline 里）
- 内部可用 CACHE.get_html/put_html

### engine/qa.py 🆕 v6（对账闸门）
```
def run_qa(dialogue, timeline=None, strict=False,
           min_msg_dur=0.3, max_overlap=0.3, tail_gap=0.2) -> dict
def format_qa_report(result) -> str
def build_replicate_prompt(reference_note="") -> str   # 复刻模式拆解提示词
```
- 在 Timeline 之后、Render 之前运行（QAGateNode）
- 检查：时间窗有效性 / 相邻气泡重叠 / 表情包&音效素材存在性 / 旁白&无音频 / 总时长尾留
- 返回 {ok, abort, errors, warnings, issues, report, stats}；strict=True 时 error 置 abort
- 不复写音频/视觉决策，只审计（符合「闸门思维：错误拦在生成前」）

### engine/library.py 🆕 v6（资产沉淀）
```
def recommend_music(dialogue) -> dict        # 长叙事→Mureka / 短快→Suno / 魔性预设
def archive_project(dialogue, qa_result=None) -> str   # 归档到 assets/library/
def list_library() -> list
```
- 每次运行把剧本结构化信息写入 `assets/library/library_index.json`（纯本地 JSON，零依赖）
- 音乐策略常量：`MUSIC_STRATEGY` / `SUNO_PRESETS`（gospel_funk/ballad/rap/pop/catchy_meme）

### engine/alignment.py（v6 ASR 优先）
- `AlignmentEngine(mode, prefer_asr=True)`：AUTO 有音频时优先 ASR（faster-whisper small，毫秒级），失败降级 VAD，再降级文本估算
- 新增 `_asr_align_dialogue` / `run_asr` / `asr_align`（下沉自 ab_generator 的 faster-whisper 实现）
- ASR 模式不再 `NotImplementedError`，可直接 `mode="ASR"` 使用

## 兼容层（保旧入口可用）
- `dialog_parser.py`：改为 import engine 并转发同名函数（parse_dialog_text 等）
- `gospel_automator.py`：改为薄封装，调 Pipeline.execute
- `gospel_dialog.py`：CLI 不变，内部走新流水线
- `audio_aligner.py`：检测逻辑迁入 engine/alignment.py 后，保留为纯工具或删

## 目录
```
scripts/
  engine/
    __init__.py   models.py   hooks.py   cache.py
    parser.py     role_mapper.py  emotion.py
    alignment.py  audio_planner.py
    assets.py     visual_planner.py
    timeline_planner.py  pipeline.py  renderer.py
  dialog_parser.py  gospel_automator.py  gospel_dialog.py
  audio_aligner.py  random_assets.py  media_utils.py
  chat_scene_renderer.py（按 timeline 适配）
```

## Hook 点位（engine/hooks.py）
before/after：parse, role_map, emotion, audio, alignment, visual, timeline, render, assemble
