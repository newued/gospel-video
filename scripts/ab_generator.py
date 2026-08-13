# -*- coding: utf-8 -*-
"""AB 双人对话短视频 → 剪映专业版 5.9 草稿生成器

流程: AB对话JSON + 完整配音MP3 → 前置校验 → 素材加载 → faster-whisper ASR对齐
      → AI编导(规则引擎) → 数据适配 → pyJianYingDraft 剪映5.9草稿填充

用法（必填仅 2 项）:
    python ab_generator.py --dialog-json <path> --audio-mp3 <path>
    # --sticker-dir 可选，缺省用 skill 内置贴图库；音效/模板/输出目录均有内置缺省
    # 自动模式会默认导出中间JSON到草稿同目录 plan_<草稿名>.json

中间JSON驱动模式（新增）:
    python ab_generator.py --plan-json <中间JSON路径>
    # 跳过 --dialog-json/--audio-mp3 前置校验组合，直接按中间JSON生成草稿；
    # 音画同步: 文本气泡 / 贴图 / 头像 / 特效严格对齐人声时间窗 audio_start~audio_end（无尾、无顺延），
    # 避免短句时上一条 0.4s 尾巴把下一条气泡推后造成音画脱节。SFX 同样用 audio_start。
    # 与贴图尺寸位置(756px 动态缩放/气泡中心钳制)为硬约束，JSON 内值不覆盖。

环境: Python 3.9+ / win10 / 中文 UTF-8 / faster-whisper small CPU int8 /
      pyJianYingDraft (仓库 vendor/ 目录, 开源随仓库分发)
"""
import argparse
import difflib
import json
import math
import os
import re
import shutil
import sys
import time

# 契约要求：中文环境，输出统一使用 UTF-8（避免管道重定向时被 locale 编码为 GBK）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------- 库引入 ----
# pyJianYingDraft（MIT，https://github.com/ALC1995/pyJianYingDraft）随仓库分发于
# vendor/ 目录。定位顺序：环境变量 PYJYD_VENDOR 优先 → 仓库内置 vendor/。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SKILL_ROOT = os.path.dirname(_THIS_DIR)
PYJYD_VENDOR = os.environ.get("PYJYD_VENDOR") or os.path.join(_SKILL_ROOT, "vendor")
if not os.path.isdir(os.path.join(PYJYD_VENDOR, "pyJianYingDraft")):
    raise RuntimeError(
        "未找到 pyJianYingDraft 依赖库（%s）。请确认仓库 vendor/pyJianYingDraft 存在，"
        "或设置环境变量 PYJYD_VENDOR 指向其父目录（例如 vendor 目录）。" % PYJYD_VENDOR)
sys.path.insert(0, PYJYD_VENDOR)
sys.path.insert(0, os.path.join(PYJYD_VENDOR, "pyJianYingDraft"))

from pyJianYingDraft import (  # noqa: E402
    AudioMaterial, AudioSegment, ClipSettings, DraftFolder, EffectSegment,
    MaskType, ScriptFile, TextBackground, TextBorder, TextIntro, TextSegment,
    TextStyle, Timerange, TrackType, VideoMaterial, VideoSegment,
)
import pyJianYingDraft as draft  # noqa: E402  (用于 VideoSceneEffectType 枚举解析)

# ---------------------------------------------------------------- 固定常量 ----
# skill 根目录 = 本脚本(scripts/)的上一级
SKILL_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CANVAS_WIDTH = 1080           # 画面宽
CANVAS_HEIGHT = 1920          # 画面高
FPS = 30                      # 帧率（舞台规格）
# 剪映原生画面特效（video effect）开关：效果完全由 plan 中间JSON 的 effect 轨（主agent
# 语义决策）驱动，脚本只在 effect 轨有 material 时落盘，不再做任何关键词硬匹配。
NATIVE_EFFECTS_ENABLED = True
# 贴纸/音效/画面特效的语义选择统一交由主agent写进 plan.json（emoji 贴纸轨 / 音效轨 /
# effect 轨 material 字段），脚本只机械执行 plan 决策；不再保留任何关键词硬匹配开关。
# 贴图"全宽贴底"布局：底边距画布底 = 舞台高 5%；贴图宽 = 舞台宽（全屏），水平居中
STICKER_HEIGHT = 768                # 表情包最大显示高度 = 画布高 × 2/5
STICKER_BOTTOM_MARGIN_RATIO = 0.05
# 贴图缩放上限（剪映建议 scale 范围 0.1-5.0）：小图强制全宽会放大 20+ 倍导致模糊变形，
# 故 scale = min(舞台宽/素材宽, MAX_STICKER_SCALE)，超出上限时保持居中不再铺满全宽
MAX_STICKER_SCALE = 5.0
TEXT_ALIGN = {"A": 0, "B": 2}                # 左对齐 / 右对齐
DISPLAY_TAIL_SEC = 0.4        # displayEnd = audio_end + 0.4s
SFX_MAX_SEC = 1.5             # 单条音效最大时长
EFFECT_MAX_SEC = 2.5          # 单条画面特效最大时长(气泡连续显示后窗口可能很长, 特效不宜跟随拉长)
ASR_MODEL = "small"           # faster-whisper 模型
ASR_DEVICE = "cpu"
ASR_COMPUTE = "int8"

# ---------------------------- 舞台布局（画布 1080×1920）----------------------------
# 坐标系：ClipSettings.transform_x/y 单位 = 半个画布宽/高（0=居中，±1=左右/上下边缘）。
# 字号单位：剪映 style.size ≈ 画布宽百分比（1 单位 = 1080/100 = 10.8px 字高），
# 参照本机 jianying-editor skill 字幕字号 5.0（约微信 17pt 等效）。
# 旧值 24.0 会渲染成约 260px 高的字，远超气泡宽度 → 每行只容 1 字 → 竖排（根因）。
SIZE_PX_FACTOR = CANVAS_WIDTH / 100.0    # 字号单位 → 像素字高
TEXT_FONT_SIZE = 5.0                     # 微信正文字号 ≈ 17pt 等效
TEXT_LINE_HEIGHT_FACTOR = 1.25           # 行高 = 字高 × 1.25
TEXT_CHAR_WIDTH_CJK = 1.0                # 中文字符估算宽度 = 字高 × 1.0
TEXT_CHAR_WIDTH_ASCII = 0.5              # 半角字符估算宽度 = 字高 × 0.5
MSG_CENTER_Y = 0.0                       # 消息区垂直居中（气泡/文字/头像同一水平线）
BUBBLE_PAD_X = 20.0                      # 气泡水平内边距（px）
BUBBLE_PAD_Y = 18.0                      # 气泡垂直内边距（px）
# 气泡不做最小宽硬撑：短消息气泡随文字自然收窄，否则会被撑大、尾巴远离头像产生空隙。
# 气泡最大"外宽"（含尾巴的整张气泡纹理宽，单位 px）：封顶防止长消息气泡越过画布中线(540px)。
# 约束：A 气泡右缘 = BUBBLE_ANCHOR_LEFT_PX + BUBBLE_MAX_OUTER_WIDTH_PX 必须 ≤ 540；
#       B 气泡左缘 = BUBBLE_ANCHOR_RIGHT_PX − BUBBLE_MAX_OUTER_WIDTH_PX 必须 ≥ 540。
# 取 420：A 右缘=120+420=540 恰好到中线；B 左缘=960−420=540。参照「会话80」自然宽度，避免过窄。
BUBBLE_MAX_OUTER_WIDTH_PX = 420
BUBBLE_ROUND_RADIUS = 0.2                # 气泡圆角（占背景高度比例）
WECHAT_BUBBLE_COLOR = "#95EC69"          # 微信绿色消息框底色
WECHAT_TEXT_COLOR = (1, 1, 1)            # 气泡内白字（绿底对比度好）

# 聊天框样式：复用剪映内置"会话"气泡预设（参考 2026-08-13 用户草稿 会话80/会话79）。
# 机制：气泡素材(type=text_shape)写入 materials.effects，文本段通过 extra_material_refs 引用；
# 剪映按 effect_id + resource_id 在内置素材库解析（参考草稿的 path 缓存已失效，不影响渲染）。
# 语义约定：A=左侧=对方=白色气泡；B=右侧=自己=绿色气泡（符合微信聊天习惯）。
# 会话79 为白色，会话80 为绿色，因此 A→会话79，B→会话80。
CHAT_BUBBLE_ENABLED = True
BUBBLE_A = {  # A 左=对方=白色：会话79
    "id": "363DD280-6A29-47bd-840D-81592BD43DE3",
    "effect_id": "720192",
    "resource_id": "6824371748159361549",
    "name": "会话79",
}
BUBBLE_B = {  # B 右=自己=绿色：会话80
    "id": "C563EAF6-ABF8-4509-B996-93692D25BDAC",
    "effect_id": "720206",
    "resource_id": "6824371829621133831",
    "name": "会话80",
}
# 头像蒙版：矩形（圆角 50）。与用户要求"矩形蒙版(圆角50)，不要圆形蒙版"一致。
AVATAR_MASK_ROUND_CORNER = 50
AVATAR_DIAMETER = 130                    # 头像直径 ≈ 画布宽 12%
AVATAR_LEFT_PX = 20                      # A 头像左边缘（px）
AVATAR_RIGHT_PX = 1060                   # B 头像右边缘（px）
AVATAR_TRANSFORM_Y = MSG_CENTER_Y        # 头像与气泡同一水平线垂直居中
# 气泡与头像的间隙（px）：设为负值 = 尾巴故意叠到头像上少许，保证"紧贴头像"且不因
# 文字宽度估算误差而在两者间留出空隙。−30 表示气泡内缘压住头像外缘 30px。
BUBBLE_GAP_PX = -30
# 气泡水平锚定（内侧边缘 = 头像边缘 + 间距，向画布中部方向生长）
BUBBLE_ANCHOR_LEFT_PX = AVATAR_LEFT_PX + AVATAR_DIAMETER + BUBBLE_GAP_PX
BUBBLE_ANCHOR_RIGHT_PX = AVATAR_RIGHT_PX - AVATAR_DIAMETER - BUBBLE_GAP_PX

# 剪映内置「会话」气泡纹理几何（huihua80/79 预览图均为 300x64）
# textRect 决定文字在气泡内的相对位置；气泡随文字等比缩放。
BUBBLE_TEXTURE_W = 300.0                 # 气泡纹理宽度（px）
BUBBLE_TEXTURE_H = 64.0                  # 气泡纹理高度（px）
BUBBLE_TEXTRCT_W = 241.0                 # 文字区宽度（两个会话均为 241）
BUBBLE_TEXTRCT_79 = [45.0, 13.0, 241.0, 35.0]   # 会话79(白色/对方/A/左)：尾巴在左
BUBBLE_TEXTRCT_80 = [12.0, 13.0, 241.0, 35.0]   # 会话80(绿色/自己/B/右)：尾巴在右
BUBBLE_WIDTH_SCALE = BUBBLE_TEXTURE_W / BUBBLE_TEXTRCT_W   # 气泡宽 / 实际文字宽 ≈1.245
# 文字中心到气泡内侧边缘的偏移比例（内侧 = 靠近头像的那一侧）
BUBBLE_A_TEXT_RATIO = (BUBBLE_TEXTRCT_79[0] + BUBBLE_TEXTRCT_79[2] / 2.0) / BUBBLE_TEXTURE_W   # ≈0.552
BUBBLE_B_TEXT_RATIO = (BUBBLE_TEXTURE_W - (BUBBLE_TEXTRCT_80[0] + BUBBLE_TEXTRCT_80[2] / 2.0)) / BUBBLE_TEXTURE_W  # ≈0.558

# 素材/模板内置缺省（不传 --sticker-dir/--sound-dir/--template-dir 时使用）
DEFAULT_STICKER_DIR = os.path.join(SKILL_ROOT, "assets", "emojis")
DEFAULT_SOUND_DIR = os.path.join(SKILL_ROOT, "assets", "sfx")
DEFAULT_TEMPLATE_DIR = os.path.join(SKILL_ROOT, "assets", "blank_template")


def default_draft_root():
    """返回剪映默认草稿目录（跨平台，--output-draft 缺省时使用）。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA", "")
        if base:
            return os.path.join(base, "JianyingPro", "User Data",
                                "Projects", "com.lveditor.draft")
        # LOCALAPPDATA 缺失时回退到用户目录下的常见安装位置
        return os.path.join(os.path.expanduser("~"),
                            "AppData", "Local", "JianyingPro",
                            "User Data", "Projects", "com.lveditor.draft")
    # macOS 剪映草稿目录
    return os.path.join(os.path.expanduser("~"), "Movies", "JianyingPro",
                        "User Data", "Projects", "com.lveditor.draft")

# ---------------------------------------------------------------- 校验错误 ----
# 与契约逐字一致
ERR_MISSING_PARAMS = "错误：缺少必要输入参数，请补齐【dialog_json_path、full_audio_mp3_path】"
ERR_BAD_DIALOG_PATH = "错误：对话文本路径无效，请检查文件是否存在且后缀为.json"
ERR_BAD_AUDIO_PATH = "错误：配音音频文件不存在，请核对MP3路径"
ERR_NO_STICKER_DIR = "错误：贴图素材库目录不存在"
ERR_NO_SOUND_DIR = "错误：音效素材库目录不存在"
ERR_BAD_TEMPLATE = "错误：剪映5.9空白草稿模板不完整，请确认模板目录"
ERR_OUTPUT_DIR = "错误：输出目录无法创建，请检查路径权限"
ERR_DIALOG_FORMAT = "错误：对话文本格式异常，仅支持[{\"role\":\"A\",\"content\":\"xxx\"},{\"role\":\"B\",\"content\":\"xxx\"}]结构，角色仅限A、B"
ERR_BAD_PLAN_PATH = "错误：中间JSON文件不存在或后缀非.json"
ERR_BAD_PLAN = "错误：中间JSON格式异常，缺少meta/tracks或结构错误"
ERR_PLAN_ALIGN = "错误：中间JSON缺少音频时长信息（tracks 无 audio 段）"


def fail(msg):
    """打印错误消息并退出。"""
    print(msg)
    sys.exit(1)


# ---------------------------------------------------------------- 编导词表 ----
QUESTION_WORDS = ["？", "?", "吗", "呢", "什么", "怎么", "为啥", "为什么",
                  "哪个", "哪些", "谁", "是否", "能不能", "可以吗", "怎么办"]
EMOTION_WORDS = ["卧槽", "哈哈", "哇", "天哪", "唉", "啊", "厉害", "佩服", "牛",
                 "气死", "生气", "哭", "笑", "吓", "惊", "讨厌", "烦", "太"]

# 注：贴纸/音效/特效的语义选择不再由脚本关键词硬匹配，统一交由主agent在 plan 中间JSON
# 的 sticker / audio(音效) / effect 轨写入决策（material 字段），脚本只机械执行。

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")


# ---------------------------------------------------------------- 工具函数 ----
def scan_files(folder, exts):
    """递归扫描目录下的指定后缀文件，返回绝对路径列表。"""
    out = []
    for root, _, names in os.walk(folder):
        for nm in names:
            if nm.lower().endswith(exts):
                out.append(os.path.join(root, nm))
    return out


def normalize_text(s):
    """去除空白与标点，仅保留文字，用于相似度比较。"""
    s = re.sub(r"[\s，。！？、,.!?；;：:“”\"'‘’（）()《》<>…—\-~～·\[\]【】]", "", s)
    return s


def load_dialogs(path):
    """读取并校验 AB 对话 JSON，返回 ([(role, content), ...], role_map)。

    role_map: {原始角色名: 'A'/'B'}，用于中文角色名自动映射。
    支持 A/B 直接使用，也支持中文名（如"贾总"/"王工"）自动按首次出现顺序映射。
    """
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except Exception:
        fail(ERR_DIALOG_FORMAT)
    if not isinstance(data, list):
        fail(ERR_DIALOG_FORMAT)
    dialogs = []
    role_map = {}  # 原始角色名 → A/B
    ab_counter = 0  # 已分配的 A/B 槽位
    for item in data:
        if not isinstance(item, dict) or "role" not in item or "content" not in item:
            fail(ERR_DIALOG_FORMAT)
        raw_role = str(item["role"]).strip()
        content = str(item["content"]).strip()
        if not content:
            fail(ERR_DIALOG_FORMAT)
        # 转录时间戳前缀清洗: "22:01 这个按钮改红色。" → "这个按钮改红色。"
        # 既保证气泡显示干净，也让 ASR 相似度匹配不受 "2201" 数字干扰。
        content = re.sub(r"^\d{1,2}[:：]\d{2}\s*", "", content).strip()
        if not content:
            fail(ERR_DIALOG_FORMAT)
        # 自动映射角色名到 A/B
        upper = raw_role.upper()
        if upper in ("A", "B"):
            role = upper
            if raw_role not in role_map:
                role_map[raw_role] = upper
        else:
            # 中文或其他角色名：按首次出现顺序映射到 A/B
            if raw_role not in role_map:
                if ab_counter >= 2:
                    fail("对话JSON角色超过2个（%s），AB模式仅支持双人对话" % raw_role)
                role = "A" if ab_counter == 0 else "B"
                role_map[raw_role] = role
                ab_counter += 1
            else:
                role = role_map[raw_role]
        dialogs.append((role, content))
    if not dialogs:
        fail(ERR_DIALOG_FORMAT)
    # 打印角色映射（便于调试）
    if any(k not in ("A", "B") for k in role_map):
        mapped = ", ".join("%s→%s" % (k, v) for k, v in role_map.items())
        print("角色映射：%s" % mapped)
    return dialogs, role_map


# ---------------------------------------------------------------- Step2 ASR ----
def run_asr(mp3_path):
    """faster-whisper small CPU int8 转写 mp3，返回 [{start,end,text}] 秒级列表。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        raise RuntimeError("faster-whisper 未安装，无法进行ASR对齐：%s" % e)
    try:
        model = WhisperModel(ASR_MODEL, device=ASR_DEVICE, compute_type=ASR_COMPUTE)
    except Exception as e:
        raise RuntimeError(
            "ASR模型加载失败，请检查 faster-whisper 安装与模型缓存（%s/%s/%s）：%s"
            % (ASR_MODEL, ASR_DEVICE, ASR_COMPUTE, e))
    try:
        # initial_prompt 用简体中文 bias 输出（faster-whisper 对 zh 默认倾向繁体，
        # 繁体 vs 简体字符无法匹配，会导致下方 asr_align 相似度全 0 而退化为字数插值）。
        # word_timestamps=True: 输出词级时间戳。段级边界跨进程不稳定(10/8/6 段反复横跳)，
        # 而词级时间戳稳定且精细，asr_align 优先用词作为对齐单元，段仅作兜底。
        seg_iter, _info = model.transcribe(
            mp3_path, language="zh",
            initial_prompt="以下是简体中文的日常对话记录，请用简体中文输出。",
            word_timestamps=True)
        segs = []
        for s in seg_iter:
            text = (s.text or "").strip()
            if text:
                segs.append({
                    "start": float(s.start), "end": float(s.end), "text": text,
                    "words": [{"start": float(w.start), "end": float(w.end),
                               "text": (w.word or "").strip()}
                              for w in (s.words or [])],
                })
        return segs
    except Exception as e:
        raise RuntimeError("ASR转写失败：%s" % e)


def asr_align(dialogs, asr_segs, total_duration):
    """将 ASR 分段时间戳对齐到 AB 对话文本（词级动态规划，抗转写噪声）。

    dialogs: [(role, content)]（content 已去转录时间戳）;
    asr_segs: [{start,end,text,words}] 按时间序;
    返回 [{role,text,audio_start,audio_end}]。

    【为什么词级 + DP】配音常见三类噪声:
      1. 转写错误/繁简/口误（"扣你绩效"→"扣你底下"、"设计稿"→"设计高"）;
      2. 两句连读被合并（"扣你绩效！扣吧，反正也没多少"是一整段）;
      3. 口误重复/填充多出片段（"非要吵"重复两遍、"但我不敢"重复、"嘿"填充）。
    旧版贪心(向后合并≤3段、不跳垃圾段)会被噪声带偏 → 时间窗漂移 → 音画不同步。
    faster-whisper 段级边界跨进程不稳定(同一音频可能出 10/8/6 段)，但词级时间戳
    稳定且精细 —— 故以「词」为对齐单元:
      - 每条对话匹配一段连续词(1~24 个)，代价 = 1 - 文本相似度(低于阈值不可行);
      - 词可被跳过(填充/重复/幻觉)，代价 SKIP; 句可未命中(插值兜底)，代价 MISS;
      - 全局 DP 取总代价最小路径，回溯得到每句命中的词区间;
      - 命中句窗口 = 首词.start ~ 末词.end; 未命中句在前后锚点间按字符数插值。
    若模型未输出词级时间戳，则退化为段级 DP(每句 1~3 段)。

    【卡拉OK式重录】返回列表可能多于 dialogs: 同句被再次演唱(重录/反复)时, 每个出现
    片段独立成一条(同 role/text, 不同时间窗), 且全部片段按时间序连续化(end = 下个
    片段 start, 最后一条到音频末尾) —— 重录时气泡重新弹出, 画面全程不中断、不重叠。
    """
    results = []

    # 兜底: 无任何转写结果 → 按文本长度均匀分配整段音频时间
    if not asr_segs:
        total_chars = sum(len(c) for _, c in dialogs) or 1
        cursor = 0.0
        for role, content in dialogs:
            dur = max(0.3, len(content) / total_chars * total_duration)
            results.append({"role": role, "text": content,
                            "audio_start": round(cursor, 3),
                            "audio_end": round(min(cursor + dur, total_duration), 3)})
            cursor = min(cursor + dur, total_duration)
        return results

    nd = [normalize_text(content) for _, content in dialogs]
    M = len(dialogs)

    # ---- 对齐单元: 优先词级(细粒度、稳定), 无词则退回段级 ----
    use_words = any(s.get("words") for s in asr_segs)
    units = []                     # [{"start","end","text"}, ...] 按时间序
    if use_words:
        for s in asr_segs:
            for w in s["words"]:
                t = w.get("text") or ""
                if t and normalize_text(t):
                    units.append({"start": float(w["start"]),
                                  "end": float(w["end"]), "text": t})
    else:
        units = [{"start": float(s["start"]), "end": float(s["end"]),
                  "text": s["text"]} for s in asr_segs]

    N = len(units)
    unit_norm = [normalize_text(u["text"]) for u in units]
    SKIP = 0.03          # 跳过一段垃圾(填充/重复/幻觉); 必须足够廉价, 否则 DP 宁可吞掉垃圾段
    MISS = 0.70          # 一句未命中(插值兜底); 高于弱匹配代价, 让弱命中优先于插值
    MIN_RATIO = 0.12     # 相似度低于此值视为不可匹配
    MAX_K = 24 if use_words else 3

    def run_cost(line_idx, start, end):
        """消耗单元 [start,end) 的代价 = 文本不相似度 + 时长超支惩罚。

        时长惩罚: 配音句长通常 ≈ 字数×0.25s; 匹配窗口超过预期×1.5 的部分
        按 0.3/s 加价, 防止长句把中间夹带的重复/口误段一起吞进去(如第8句
        吞掉前一句重复的"非要吵")。"""
        merged = "".join(unit_norm[start:end])
        if not merged:
            return float("inf")
        r = difflib.SequenceMatcher(None, nd[line_idx], merged).ratio()
        if r < MIN_RATIO:
            return float("inf")
        dur = float(units[end - 1]["end"]) - float(units[start]["start"])
        exp = max(0.6, len(nd[line_idx]) * 0.25)
        over = max(0.0, dur - exp * 1.5)
        return (1.0 - r) + over * 0.3

    INF = float("inf")
    dp = [[INF] * (N + 1) for _ in range(M + 1)]
    back = [[None] * (N + 1) for _ in range(M + 1)]   # ('skip',) / ('miss',) / ('run', k)
    dp[0][0] = 0.0
    for j in range(1, N + 1):                          # 前导单元全部跳过
        dp[0][j] = dp[0][j - 1] + SKIP
        back[0][j] = ("skip",)
    for i in range(1, M + 1):
        for j in range(N + 1):
            best, bk = INF, None
            if dp[i - 1][j] + MISS < best:             # 未命中(插值兜底)
                best, bk = dp[i - 1][j] + MISS, ("miss",)
            if j >= 1 and dp[i][j - 1] + SKIP < best:   # 跳过单元 j-1
                best, bk = dp[i][j - 1] + SKIP, ("skip",)
            for k in range(1, min(MAX_K, j) + 1):      # 消耗单元 [j-k, j)
                c = run_cost(i - 1, j - k, j)
                if c == INF:
                    continue
                v = dp[i - 1][j - k] + c
                if v < best:
                    best, bk = v, ("run", k)
            dp[i][j] = best
            back[i][j] = bk

    # ---- 回溯: 每句命中单元索引列表(升序) ----
    match_run = [[] for _ in range(M)]
    i, j = M, N
    while i > 0 and j > 0:
        bk = back[i][j]
        if bk is None:
            break
        kind = bk[0]
        if kind == "skip":
            j -= 1
        elif kind == "miss":
            i -= 1
        else:
            k = bk[1]
            for x in range(j - k, j):
                match_run[i - 1].append(x)
            j -= k
            i -= 1
    # 循环停止时: 若 j>0(尾部单元未消费)即全部视为跳过, 不影响窗口。

    # ---- 由命中关系计算每句的"主窗口" ----
    # matched[li] = 该句主窗口（命中句=首词.start~末词.end；未命中句=插值窗口）
    matched = [None] * M
    for li in range(M):
        if match_run[li]:
            s0, e1 = match_run[li][0], match_run[li][-1]
            matched[li] = (float(units[s0]["start"]), float(units[e1]["end"]))

    for li in range(M):
        if matched[li]:
            continue
        p = li - 1
        while p >= 0 and not matched[p]:
            p -= 1
        q = li + 1
        while q < M and not matched[q]:
            q += 1
        span_start = matched[p][1] if p >= 0 else 0.0
        if q < M:
            span_end = matched[q][0]
            block = list(range(li, q))
        else:
            span_end = total_duration
            block = list(range(li, M))
        chars = [len(nd[x]) + 0.5 for x in block]
        total_c = sum(chars)
        avail = span_end - span_start
        if avail <= 0.02 and q < M:
            # 锚点紧贴: 与下一命中句共享同一段, 按字符比例切分该段窗口
            avail = matched[q][1] - matched[q][0]
            span_start = matched[q][0]
            chars_all = chars + [len(nd[q]) + 0.5]
            total_c = sum(chars_all)
            cursor = span_start
            for idx, x in enumerate(block):
                dur = avail * (chars_all[idx] / total_c) if total_c else avail / len(chars_all)
                matched[x] = (cursor, cursor + dur)
                cursor += dur
            matched[q] = (cursor, matched[q][1])
            continue
        cursor = span_start
        for idx, x in enumerate(block):
            dur = (avail * (chars[idx] / total_c)) if total_c else (avail / len(block))
            matched[x] = (cursor, min(cursor + dur, total_duration))
            cursor += dur

    # ---- 重录片段检测: 未被任何句匹配的词里找"同句重唱"片段 ----
    # 卡拉OK式: 重录片段是**独立出现**, 气泡在重录时重新弹出, 而不是并入首现窗口一直挂着。
    # 做法: skipped 词按时间分块, 每句在每块内滑窗找相似子片段(ratio/覆盖率达标),
    # 词不重用(先到先得), 命中的片段记为该句的一次独立出现。
    REPEAT_RATIO = 0.5    # 相似度门槛: 只认"整句/句尾实质重唱"(1.0/0.55), 排除碎片混搭(0.33~0.4)
    REPEAT_COVER = 0.3    # 覆盖率: 重复片段长度须 ≥ 该句长度×30%, 防止抢走零散单字
    matched_units = set()
    for _run in match_run:
        matched_units.update(_run)
    blocks = []                          # skipped 词按时间连续分组
    cur = []
    for u in range(N):
        if u in matched_units:
            if cur:
                blocks.append(cur)
                cur = []
        else:
            cur.append(u)
    if cur:
        blocks.append(cur)
    used = set()
    repeat_frags = [[] for _ in range(M)]   # 每句的重录片段 [(start, end), ...]
    for li in range(M):
        if not match_run[li]:
            continue
        line_len = len(nd[li])
        min_frag = max(2, int(math.ceil(line_len * REPEAT_COVER)))
        for blk in blocks:
            free = [u for u in blk if u not in used]
            if not free:
                continue
            best = None                   # (ratio, a, b) 在 free 上的最佳连续子片段
            for a in range(len(free)):
                for b in range(a + 1, min(a + 13, len(free)) + 1):
                    idxs = free[a:b]
                    merged = "".join(unit_norm[x] for x in idxs)
                    if len(merged) < min_frag:
                        continue
                    r = difflib.SequenceMatcher(None, nd[li], merged).ratio()
                    if r >= REPEAT_RATIO and (best is None or r > best[0]):
                        best = (r, a, b)
            if best:
                idxs = free[best[1]:best[2]]
                used.update(idxs)
                repeat_frags[li].append(
                    (float(units[idxs[0]]["start"]), float(units[idxs[-1]]["end"])))

    # ---- 组装全部"出现片段"(主窗口 + 重录), 全局时间序, 连续化 ----
    # 连续化 = 每个片段显示到下一个片段出现为止(最后延伸到音频末尾), 保证画面不中断;
    # 片段间天然无缝(end == 下个 start), 只有重录延伸超过下个 start 才重叠(分层轨承载)。
    fragments = []                        # (start, end, line_idx)
    for li in range(M):
        if matched[li]:
            fragments.append((matched[li][0], matched[li][1], li))
        for s, e in repeat_frags[li]:
            fragments.append((s, e, li))
    fragments.sort(key=lambda x: x[0])
    for fi in range(len(fragments)):
        _s, _e, _li = fragments[fi]
        if fi + 1 < len(fragments):
            _e = max(_e, fragments[fi + 1][0])
        else:
            _e = max(_e, total_duration)
        fragments[fi] = (_s, _e, _li)

    # 输出: 每次出现一条 aligned(重录片段与主片段同 role/text, 仅窗口不同)
    for s, e, li in fragments:
        role, content = dialogs[li]
        ws = max(0.0, min(s, total_duration))
        we = max(ws + 0.1, min(e, total_duration))
        results.append({"role": role, "text": content,
                        "audio_start": round(ws, 3),
                        "audio_end": round(we, 3)})
    return results


# ---------------------------------------------------------------- Step3 编导 ----
def pick_animation(role, text, prev_anim):
    """选择入场动画: 疑问→弹性弹出; 感叹/情绪→侧向滑入; 否则淡入。相邻不重复。"""
    if any(q in text for q in QUESTION_WORDS):
        anim = TextIntro.弹入                      # 弹性弹出
    elif "！" in text or "!" in text or any(e in text for e in EMOTION_WORDS):
        anim = TextIntro.向右滑动 if role == "A" else TextIntro.向左滑动   # 侧向滑入
    else:
        anim = TextIntro.渐显                      # 淡入
    # 相邻不重复: 与上一条相同时按 淡入→侧滑→弹性 轮换
    if anim == prev_anim:
        if role == "A":
            order = [TextIntro.渐显, TextIntro.向右滑动, TextIntro.弹入]
        else:
            order = [TextIntro.渐显, TextIntro.向左滑动, TextIntro.弹入]
        anim = order[(order.index(anim) + 1) % len(order)]
    return anim


# 贴纸/音效选择已上移为主agent在 plan 中间JSON 的 sticker / audio(音效) 轨决策，
# 脚本不再做关键词硬匹配（match_sticker/match_sfx/match_effect 已移除）。



# ---------------------------------------------------------------- 画面特效 ----
# 画面特效选择已上移为主agent在 plan 中间JSON 的 effect 轨决策（material 填剪映
# 特效 identifier 字符串，如 "震动"/"啊啊啊啊"），脚本只经 _resolve_video_effect
# 解析落盘，不再做关键词硬匹配。


def _resolve_video_effect(name):
    """把中文特效名解析为 pyJianYingDraft VideoSceneEffectType 枚举成员。

    复刻 jianying-editor 的 resolve_enum_with_synonyms：精确名 → 大小写不敏感
    → 同义词 → 模糊匹配；并复用 effects_catalog.normalize_effect 做口语归一化。
    解析失败返回 None（调用方跳过并打印提示）。
    """
    if not name:
        return None
    try:
        from engine.effects_catalog import normalize_effect
    except Exception:
        normalize_effect = lambda x: x
    n = normalize_effect(name)
    enum_cls = draft.VideoSceneEffectType
    if hasattr(enum_cls, n):
        return getattr(enum_cls, n)
    nl = n.lower()
    mapping = {k.lower(): k for k in enum_cls.__members__.keys()}
    if nl in mapping:
        return getattr(enum_cls, mapping[nl])
    import difflib
    close = difflib.get_close_matches(n, enum_cls.__members__.keys(), n=1, cutoff=0.6)
    if close:
        return getattr(enum_cls, close[0])
    return None


# ---------------------------------------------------------------- Step4 适配 ----
def build_timeline(aligned, total_duration):
    """生成时间线: 音画同步（display_start=audio_start, display_end=audio_end，无尾无顺延）。"""
    messages = []
    for i, m in enumerate(aligned):
        # 直接用人声时间窗，避免短句时上一条 0.4s 尾巴把下一条推后导致音画脱节
        m2 = dict(m)
        ds = m["audio_start"]
        de = m["audio_end"]
        # 唱词反复/重录会把窗口扩展到相邻句(对唱场景允许气泡重叠), 只防倒序与零宽
        if i > 0:
            prev_start = aligned[i - 1]["audio_start"]
            if ds < prev_start:
                ds = prev_start
        if de <= ds:
            de = ds + 0.3
        m2["display_start"] = ds
        m2["display_end"] = de
        messages.append(m2)
    duration = total_duration
    return {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT, "fps": FPS,
            "duration": round(duration, 3), "messages": messages}


# ------------------------------------------------------------ 微信样式辅助 ----
def find_avatar_image(dialog_json_path, role):
    """从 dialog.json 同目录找角色头像: A.png/A.jpg/a.png 等, 找不到返回 None。"""
    d = os.path.dirname(dialog_json_path) or "."
    for name in (role + ".png", role + ".jpg", role + ".jpeg",
                 role.lower() + ".png", role.lower() + ".jpg"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def find_background_image(dialog_json_path):
    """从 dialog.json 同目录找聊天背景图: BG.png/BG.jpg/bg.png 等, 找不到返回 None。"""
    d = os.path.dirname(dialog_json_path) or "."
    for name in ("BG.png", "BG.jpg", "BG.jpeg", "bg.png", "bg.jpg"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def text_width_px(text, size=None):
    """估算文本宽度(px): 剪映字号单位 ≈ 画布宽百分比, 中文宽=字高, 半角=0.5 字高。"""
    size = size if size is not None else TEXT_FONT_SIZE
    char_px = size * SIZE_PX_FACTOR
    w = 0.0
    for ch in text:
        w += char_px * (TEXT_CHAR_WIDTH_CJK if ord(ch) > 0x2E80 else TEXT_CHAR_WIDTH_ASCII)
    return w


def calc_bubble_layout(role, text):
    """微信气泡布局: 返回 (bg_width, bg_height, transform_x, max_line_width, center_px)。

    内置「会话」气泡(text_shape)随文字等比缩放: 气泡外宽 = 文字行宽 × BUBBLE_WIDTH_SCALE
    (纹理 300 / 文字区 241)。定位以「气泡内侧边缘(尾巴)紧贴头像」为准:
      A(会话79/白/左): 气泡左缘 = BUBBLE_ANCHOR_LEFT_PX(头像右缘, 已含 −30px 重叠)
         center_px = 左缘 + BUBBLE_A_TEXT_RATIO × bubble_w
      B(会话80/绿/右): 气泡右缘 = BUBBLE_ANCHOR_RIGHT_PX(头像左缘, 已含 −30px 重叠)
         center_px = 右缘 − BUBBLE_B_TEXT_RATIO × bubble_w
    不做最小宽硬撑(否则短消息气泡被撑大、尾巴离头像产生空隙); 仅按 BUBBLE_MAX_OUTER_WIDTH_PX
    封顶, 保证长消息气泡外宽 ≤ 中线约束(右缘 ≤ 540 / 左缘 ≥ 540), 不出现白绿左右错配。
    center_px 为文字中心像素 x, 供贴图在气泡正下方居中。
    """
    est = text_width_px(text)
    # 文字行宽(像素): 自然宽度, 仅受气泡最大外宽封顶(避免越过中线)
    line_text_w = min(est, BUBBLE_MAX_OUTER_WIDTH_PX / BUBBLE_WIDTH_SCALE)
    # 气泡外宽 = 文字行宽 × 纹理缩放比(300/241), 并封顶
    bubble_w = min(line_text_w * BUBBLE_WIDTH_SCALE, BUBBLE_MAX_OUTER_WIDTH_PX)

    if role == "A":
        # A: 白色/会话79, 尾巴在左, 气泡左缘紧贴左头像右缘(含重叠)
        center_px = BUBBLE_ANCHOR_LEFT_PX + BUBBLE_A_TEXT_RATIO * bubble_w
    else:
        # B: 绿色/会话80, 尾巴在右, 气泡右缘紧贴右头像左缘(含重叠)
        center_px = BUBBLE_ANCHOR_RIGHT_PX - BUBBLE_B_TEXT_RATIO * bubble_w
    t_x = (center_px - CANVAS_WIDTH / 2) / (CANVAS_WIDTH / 2)

    # 文本换行宽度(占画布宽比例): 与气泡内文字区一致, 避免文字溢出气泡
    max_line_w = line_text_w / CANVAS_WIDTH
    lines = max(1, int(math.ceil(est / line_text_w))) if line_text_w > 0 else 1
    box_h = (lines * TEXT_FONT_SIZE * SIZE_PX_FACTOR * TEXT_LINE_HEIGHT_FACTOR
             + 2 * BUBBLE_PAD_Y)
    return (line_text_w / CANVAS_WIDTH, box_h / CANVAS_HEIGHT,
            round(t_x, 4), round(max_line_w, 4), center_px)


def calc_sticker_layout(mat):
    """贴图布局：返回 (scale_x, scale_y, transform_x, transform_y)。

    JianYing 缩放语义（用户确认）：
      - scale_x = 显示宽度 / 舞台宽度 CANVAS_WIDTH(1080px)
        100% = 图片宽度铺满舞台宽度
      - scale_y = 显示高度 / (显示宽度 × 原始高/原始宽)
        100% = 保持原始宽高比
      - 当等比缩放关闭时，scale_x 和 scale_y 独立控制

    实现（用户要求：表情包高度 800px，宽度等比例缩放）：
      1. 目标高度 = STICKER_HEIGHT(800px)
      2. 等比缩放：显示宽度 = 目标高度 × 原始宽/原始高
      3. scale_x = 显示宽度 / 舞台宽度
         scale_y = 1.0（保持原始宽高比，高度恰好为 800px）
      4. 超高保护：若目标高度 > 可用高度(CANVAS_HEIGHT*0.95)，
         则以可用高度为约束回缩，重新计算 scale_x/scale_y
      5. 水平居中：transform_x = 0.0
      6. 垂直贴底：贴图底边距画布底 5% 舞台高
         像素坐标：底边 y = CANVAS_HEIGHT*0.95；
                   中心 y = CANVAS_HEIGHT*0.95 - 显示高度/2；
         换算 transform_y = (中心y - CANVAS_HEIGHT/2) / (CANVAS_HEIGHT/2)
    """
    raw_w = float(mat.width) if mat.width is not None else 500.0
    raw_h = float(mat.height) if mat.height is not None else 500.0
    usable_h = CANVAS_HEIGHT * (1.0 - STICKER_BOTTOM_MARGIN_RATIO)   # 可用高 = 画布高 95%

    # 目标高度 800px，等比缩放计算显示宽度
    target_h = min(STICKER_HEIGHT, usable_h)   # 超高保护
    disp_h = target_h
    disp_w = target_h * raw_w / raw_h          # 等比：宽度 = 高度 × 原始宽高比

    # JianYing 缩放语义：scale_x = 显示宽度 / 舞台宽度
    # 100% = 舞台宽度(1080px)，scale_x ≤ 1.0
    # scale_y 必须等于 scale_x，否则表情包变形
    scale_x = disp_w / CANVAS_WIDTH               # ≤ 1.0
    scale_y = scale_x                              # 等比缩放，防止变形

    # 垂直贴底（y值越大越往上，y值越小越往下）
    center_y = usable_h - disp_h / 2.0
    # 坐标系：y>0 向上，y<0 向下；贴底需要负值
    ty = (CANVAS_HEIGHT / 2.0 - center_y) / (CANVAS_HEIGHT / 2.0)
    return (round(scale_x, 4), round(scale_y, 4), 0.0, round(ty, 4))


# ---------------------------------------------------------------- Step5 草稿 ----
def _assign_layers(intervals):
    """区间分层(着色): 返回每段的层号(0基), 同一层内互不重叠。

    唱词反复/重录会生成互相重叠的时间窗(如第6句重录段与第7句主段交叠), 而剪映
    同一轨道不允许片段重叠 → 把重叠的消息放到独立轨道(层)。intervals 需按时间序。
    """
    layers = []                     # 每层当前最后结束时间
    res = [0] * len(intervals)
    for i, (s, e) in enumerate(intervals):
        placed = -1
        for li, last_end in enumerate(layers):
            if s >= last_end - 1e-6:
                placed = li
                break
        if placed < 0:
            layers.append(e)
            placed = len(layers) - 1
        else:
            layers[placed] = max(layers[placed], e)
        res[i] = placed
    return res


def _layer_track(base, layer):
    """第 layer 层(0基)的轨道名: 第0层用原名, 后续层加序号(气泡文本轨2/3...)。"""
    return base if layer == 0 else "%s%d" % (base, layer + 1)


def init_script(template_dir, draft_name):
    """从模板目录初始化 ScriptFile。

    模板目录含 draft_content.json/draft_info.json → ScriptFile.load_template 加载;
    否则 → DraftFolder(template_dir).create_draft(...) 从零生成。
    """
    tpl_content = os.path.join(template_dir, "draft_content.json")
    tpl_info = os.path.join(template_dir, "draft_info.json")
    if os.path.isfile(tpl_content) or os.path.isfile(tpl_info):
        src = tpl_content if os.path.isfile(tpl_content) else tpl_info
        sf = ScriptFile.load_template(src)
        sf.fps = FPS   # 模板可能为 60fps, 按舞台规格统一为 30fps
    else:
        df = DraftFolder(template_dir)
        sf = df.create_draft(draft_name, CANVAS_WIDTH, CANVAS_HEIGHT, fps=FPS)
    return sf


def fill_draft(sf, timeline, mp3_path, total_us, avatar_files=None, bg_image=None):
    """填充剪映草稿: 背景(可选) + 音频轨 + 文本轨(微信气泡) + 贴图轨 + 头像轨 + 音效轨。"""
    # ---- 先建全部轨道(保证图层顺序); 背景轨为最底层视频轨, 规避剪映对齐0s限制 ----
    sf.add_track(TrackType.video, "背景轨")                       # render 0 最底层
    sf.add_track(TrackType.audio, "音频轨道")                     # render 0
    sf.add_track(TrackType.video, "贴图轨", relative_index=5)     # render 5
    sf.add_track(TrackType.video, "头像轨", relative_index=6)     # render 6
    sf.add_track(TrackType.text, "气泡文本轨", relative_index=1)  # render 15001
    sf.add_track(TrackType.audio, "音效轨", relative_index=1)     # render 1

    # ---- 0) 背景轨: 聊天背景图铺满画布(可选) ----
    if bg_image:
        bg_mat = VideoMaterial(bg_image)
        # cover 模式: 等比放大铺满画布, scale = 显示尺寸/舞台尺寸
        bg_disp_h = CANVAS_HEIGHT
        bg_disp_w = int(bg_disp_h * float(bg_mat.width) / float(bg_mat.height))
        if bg_disp_w < CANVAS_WIDTH:
            bg_disp_w = CANVAS_WIDTH
            bg_disp_h = int(bg_disp_w * float(bg_mat.height) / float(bg_mat.width))
        bg_scale_x = bg_disp_w / CANVAS_WIDTH
        bg_scale_y = bg_scale_x  # 等比 cover，防止变形
        bg_seg = VideoSegment(bg_mat, Timerange(0, int(total_us)),
                              clip_settings=ClipSettings(scale_x=bg_scale_x, scale_y=bg_scale_y))
        bg_seg.uniform_scale = False   # 关闭统一缩放, 使 clip.scale.x/y 生效
        sf.add_segment(bg_seg, track_name="背景轨")

    # ---- 1) 音频轨: 完整 mp3 ----
    audio_mat = AudioMaterial(mp3_path)
    sf.add_segment(AudioSegment(audio_mat, Timerange(0, int(total_us))),
                   track_name="音频轨道")

    # ---- 2) 文本轨: 微信绿色消息框（A 靠左 / B 靠右，与头像同一水平线垂直居中）----
    # 唱词反复/重录会生成重叠时间窗 → 区间分层, 重叠消息放独立轨道(剪映同轨不允许重叠)
    t_intervals = [(float(m["display_start"]), float(m["display_end"]))
                   for m in timeline["messages"]]
    t_layers = _assign_layers(t_intervals)
    _n = (max(t_layers) + 1) if t_layers else 1
    for _li in range(1, _n):
        sf.add_track(TrackType.text, _layer_track("气泡文本轨", _li))
        sf.add_track(TrackType.video, _layer_track("贴图轨", _li))
        sf.add_track(TrackType.video, _layer_track("头像轨", _li))
        sf.add_track(TrackType.effect, _layer_track("EffectTrack", _li))

    for idx, m in enumerate(timeline["messages"]):
        start_us = int(round(m["display_start"] * 1e6))
        dur_us = int(round((m["display_end"] - m["display_start"]) * 1e6))
        role = m["role"]
        bg_w, bg_h, t_x, line_max, center_px = calc_bubble_layout(role, m["text"])
        m["_bubble"] = (bg_w, bg_h, t_x, line_max, center_px)   # 供贴图在气泡正下方居中
        # 聊天框样式：启用内置"会话"气泡时由气泡提供背景（关闭 TextBackground/描边，文字改黑）；
        # 否则维持原微信绿底白字样式。
        if CHAT_BUBBLE_ENABLED:
            text_color = (0, 0, 0)
            background = None
            border = None
        else:
            text_color = WECHAT_TEXT_COLOR
            background = TextBackground(color=WECHAT_BUBBLE_COLOR, style=1, alpha=1.0,
                                        round_radius=BUBBLE_ROUND_RADIUS,
                                        height=bg_h, width=bg_w,
                                        horizontal_offset=0.5, vertical_offset=0.5)
            border = TextBorder(color=(0, 0, 0), width=20.0)
        style = TextStyle(size=TEXT_FONT_SIZE, bold=True, color=text_color,
                          align=TEXT_ALIGN[role], auto_wrapping=True,
                          max_line_width=line_max)
        clip = ClipSettings(transform_x=t_x, transform_y=MSG_CENTER_Y)
        seg = TextSegment(m["text"], Timerange(start_us, dur_us),
                          style=style, border=border, background=background,
                          clip_settings=clip)
        if m.get("animation") is not None:
            seg.add_animation(m["animation"])
        sf.add_segment(seg, track_name=_layer_track("气泡文本轨", t_layers[idx]))

    # ---- 3) 贴图轨(视频轨): 全宽贴底（缩放后宽=舞台宽, 水平居中, 底边距画布底5%）----
    for idx, m in enumerate(timeline["messages"]):
        if not m.get("sticker"):
            continue
        mat = VideoMaterial(m["sticker"])
        scale_x, scale_y, tx, ty = calc_sticker_layout(mat)
        clip = ClipSettings(scale_x=scale_x, scale_y=scale_y,
                            transform_x=tx, transform_y=ty)
        start_us = int(round(m["display_start"] * 1e6))
        dur_us = int(round((m["display_end"] - m["display_start"]) * 1e6))
        # GIF 等短素材: source 截取不超过素材时长(慢放铺满窗口), 避免超出素材时长报错
        src_us = int(min(mat.duration, dur_us))
        sticker_seg = VideoSegment(mat, Timerange(start_us, dur_us),
                                   source_timerange=Timerange(0, src_us),
                                   clip_settings=clip)
        sticker_seg.uniform_scale = False   # 关闭统一缩放, 使 clip.scale.x/y 生效
        sf.add_segment(sticker_seg, track_name=_layer_track("贴图轨", t_layers[idx]))

    # ---- 4) 头像轨(视频轨): 圆形头像, A 左 B 右, 与气泡同一水平线垂直居中 ----
    if avatar_files:
        for idx, m in enumerate(timeline["messages"]):
            avatar = avatar_files.get(m["role"])
            if not avatar:
                continue
            amat = VideoMaterial(avatar)
            scale = AVATAR_DIAMETER / float(CANVAS_WIDTH)
            if m["role"] == "A":
                ax = (AVATAR_LEFT_PX + AVATAR_DIAMETER / 2 - CANVAS_WIDTH / 2) / (CANVAS_WIDTH / 2)
            else:
                ax = (AVATAR_RIGHT_PX - AVATAR_DIAMETER / 2 - CANVAS_WIDTH / 2) / (CANVAS_WIDTH / 2)
            start_us = int(round(m["display_start"] * 1e6))
            dur_us = int(round((m["display_end"] - m["display_start"]) * 1e6))
            clip = ClipSettings(scale_x=scale, scale_y=scale,
                                transform_x=round(ax, 4), transform_y=AVATAR_TRANSFORM_Y)
            seg = VideoSegment(amat, Timerange(start_us, dur_us), clip_settings=clip)
            seg.uniform_scale = False   # 关闭统一缩放, 使 clip.scale.x/y 生效
            # 矩形蒙版(圆角50)：矩形头像框，非圆形
            seg.add_mask(MaskType.矩形, size=1.0, rect_width=1.0,
                         round_corner=AVATAR_MASK_ROUND_CORNER)
            sf.add_segment(seg, track_name=_layer_track("头像轨", t_layers[idx]))

    # ---- 5) 音效轨: 在对应消息 audio_start 处, 相邻不重叠 ----
    last_end_us = 0
    for m in timeline["messages"]:
        if not m.get("sfx"):
            continue
        smat = AudioMaterial(m["sfx"])
        start_us = max(int(round(m["audio_start"] * 1e6)), last_end_us)
        dur_us = min(smat.duration, int(SFX_MAX_SEC * 1e6))
        end_us = min(start_us + dur_us, total_us)
        if end_us - start_us < int(0.3 * 1e6):
            end_us = min(start_us + int(0.3 * 1e6), total_us)
        if end_us <= start_us:
            continue  # 已超出音频总时长, 跳过
        sf.add_segment(AudioSegment(smat, Timerange(start_us, end_us - start_us)),
                       track_name="音效轨")
        last_end_us = end_us

    # ---- 6) 画面特效轨(剪映原生 video effect): 按消息 scene_effect 放置 ----
    # 特效时长封顶后可能与相邻特效重叠(文本窗无缝≠特效窗无缝), 按特效实际窗口重新分层。
    if NATIVE_EFFECTS_ENABLED:
        _eff_win = []
        _eff_objs = []                 # (eff_type, start_us, dur_us)
        for m in timeline["messages"]:
            eff = m.get("scene_effect")
            if not eff:
                continue
            eff_type = _resolve_video_effect(eff)
            if not eff_type:
                print("提示：画面特效「%s」未在剪映模板匹配，跳过" % eff)
                continue
            es = float(m["display_start"])
            ee = float(m["display_end"])
            dur = min(max((ee - es), 0.3), EFFECT_MAX_SEC)
            _eff_win.append((es, es + dur))
            _eff_objs.append((eff_type, int(round(es * 1e6)), int(round(dur * 1e6))))
        if _eff_objs:
            _eff_layers = _assign_layers(_eff_win)
            for (_eff_type, _start_us, _dur_us), _ly in zip(_eff_objs, _eff_layers):
                _tr = _layer_track("EffectTrack", _ly)
                try:
                    sf.add_track(TrackType.effect, _tr)
                except Exception:
                    pass
                # 用 add_effect（而非 add_segment）才会把特效素材注册到 materials.video_effects，
                # 否则剪映有轨道无素材 → 渲染不出
                sf.add_effect(_eff_type, Timerange(_start_us, _dur_us), track_name=_tr)


def save_draft(sf, template_dir, output_dir, draft_name):
    """保存草稿到 output_draft_dir/<draft_name>/, 并补齐模板辅助文件。

    额外后处理: 修复 pyJianYingDraft 中 uniform_scale.value 硬编码为 1.0 的问题。
    JianYing 5.9 的 uniform_scale.value 语义是归一化比例(0-1), 与 clip.scale.x/y
    的"倍数"(1.0=原始大小)不同。二者同时写会导致剪辑器重置 scale。修复: 关闭
    uniform_scale(on=False), 仅依赖 clip.scale.x/y 控制缩放(倍数语义, 用户确认)。
    """
    out_draft_dir = os.path.join(output_dir, draft_name)
    os.makedirs(out_draft_dir, exist_ok=True)

    tpl_content = os.path.join(template_dir, "draft_content.json")
    tpl_info = os.path.join(template_dir, "draft_info.json")
    if os.path.isfile(tpl_content) or os.path.isfile(tpl_info):
        # 路径A: load_template 模式 → 改 save_path 到输出草稿, 手动复制模板辅助文件
        sf.save_path = os.path.join(out_draft_dir, "draft_info.json")
        for name in ["draft_meta_info.json", "draft_settings", "key_value.json"]:
            src = os.path.join(template_dir, name)
            if os.path.exists(src):
                dst = os.path.join(out_draft_dir, name)
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
        sf.save()
    else:
        # 路径B: create_draft 已在模板目录生成草稿文件夹 → 保存后整体搬移到输出目录
        sf.save()
        tmp_draft = os.path.join(template_dir, draft_name)
        if os.path.isdir(tmp_draft):
            shutil.copytree(tmp_draft, out_draft_dir, dirs_exist_ok=True)
            shutil.rmtree(tmp_draft)

    # ---- 后处理: 修复 uniform_scale ----
    # pyJianYingDraft 在 segment.export_json 中将 uniform_scale.value 硬编码为 1.0.
    # 但 JianYing 5.9 的 uniform_scale.value 语义是归一化比例(0-1, 1.0=画布大小),
    # 与 clip.scale.x/y 的"倍数"(1.0=原始大小)不同。二者同时写 on=True + value=倍数
    # 会导致剪辑器 value 超出 0-1 范围重置为 1.0, 贴图显示原始尺寸(太小)或异常。
    # 修复: 关闭 uniform_scale(on=False), 仅依赖 clip.scale.x/y(倍数语义, 用户确认).
    draft_info_path = os.path.join(out_draft_dir, "draft_info.json")
    if os.path.isfile(draft_info_path):
        with open(draft_info_path, "r", encoding="utf-8") as f:
            dj = json.load(f)
        for track in dj.get("tracks", []):
            for seg in track.get("segments", []):
                clip = seg.get("clip", {})
                if not clip or "scale" not in clip:
                    continue
                # 关闭 uniform_scale, 仅用 clip.scale.x/y 控制缩放
                seg["uniform_scale"] = {"on": False, "value": 1.0}
        with open(draft_info_path, "w", encoding="utf-8") as f:
            json.dump(dj, f, ensure_ascii=False, indent=2)

    return out_draft_dir


# --------------------------------------------------------- 聊天气泡注入 ----
def _build_bubble_material(bub):
    """构造剪映内置"会话"气泡素材(type=text_shape)，字段对齐用户参考草稿。"""
    return {
        "adjust_params": [],
        "algorithm_artifact_path": "",
        "apply_target_type": 0,
        "bloom_params": None,
        "category_id": "bubble",
        "category_name": "气泡",
        "color_match_info": {
            "source_feature_path": "", "target_feature_path": "", "target_image_path": ""
        },
        "effect_id": bub["effect_id"],
        "enable_skin_tone_correction": False,
        "exclusion_group": [],
        "face_adjust_params": [],
        "formula_id": "",
        "id": bub["id"],
        "intensity_key": "",
        "multi_language_current": "",
        "name": bub["name"],
        "panel_id": "",
        # path 指向剪映本地效果缓存（仅作记录；剪映按 effect_id+resource_id 解析内置气泡，
        # 缓存缺失/他人机器无此路径不影响渲染）。跨平台动态拼接，避免硬编码本机用户名。
        "path": os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "JianyingPro", "User Data", "Cache", "effect",
            str(bub["effect_id"])).replace("\\", "/"),
        "platform": "all",
        "request_id": "20260813101619D691809991CC774835D1",
        "resource_id": bub["resource_id"],
        "source_platform": 0,
        "sub_type": "none",
        "time_range": None,
        "type": "text_shape",
        "value": 1.0,
        "version": "",
    }


def _inject_chat_bubbles_ab(draft_info_path, roles=None):
    """AB 模式草稿写盘后，注入剪映内置"会话"气泡作为聊天信息框样式。

    复用用户 2026-08-13 参考草稿的会话79(左/A=白色)/会话80(右/B=绿色)预设：将气泡素材写入
    materials.effects，并按消息角色(A/B)为每条消息挂载对应气泡（A→会话79 白色；B→会话80 绿色）。
    语义约定：A=左侧=对方=白色，B=右侧=自己=绿色（微信聊天习惯）。
    内置气泡由 effect_id+resource_id 解析，path 缓存失效不影响渲染。注入失败静默跳过。
    """
    if not CHAT_BUBBLE_ENABLED:
        return
    try:
        with open(draft_info_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print("[warn] 读取草稿失败，跳过聊天气泡注入: %s" % e)
        return

    effects = data.setdefault("materials", {}).setdefault("effects", [])
    for bub in (BUBBLE_A, BUBBLE_B):
        if any(e.get("id") == bub["id"] for e in effects):
            continue
        effects.append(_build_bubble_material(bub))
        print("[+] 已注入聊天气泡素材: %s (effect_id=%s)" % (bub["name"], bub["effect_id"]))

    # 收集文本段，顺序与 roles 列表对应
    text_segments = []
    for tr in data.get("tracks", []):
        if tr.get("type") == "text":
            text_segments.extend(tr.get("segments", []))

    injected = 0
    for i, seg in enumerate(text_segments):
        refs = seg.setdefault("extra_material_refs", [])
        # 优先使用传入的角色列表；未传时按 transform.x 符号回退（长气泡可能跨过中线，仅作兜底）
        if roles and i < len(roles):
            role = roles[i]
        else:
            tx = (seg.get("clip") or {}).get("transform", {}).get("x", 0.0)
            role = "A" if tx < 0 else "B"
        bub = BUBBLE_A if role == "A" else BUBBLE_B
        if bub["id"] not in refs:
            refs.append(bub["id"])
            injected += 1
    print("[+] 已为 %d 个文本段挂载聊天气泡" % injected)

    try:
        with open(draft_info_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("[warn] 写回草稿失败，聊天气泡未生效: %s" % e)


# ------------------------------------------------------------ 中间JSON模式 ----
# 动画类型名 ↔ TextIntro 枚举 映射（中间JSON animation.type 用字符串）
PLAN_ANIM_MAP = {
    "fade_in": TextIntro.渐显,
    "pop_in": TextIntro.弹入,
    "slide_in_right": TextIntro.向右滑动,
    "slide_in_left": TextIntro.向左滑动,
}
PLAN_ANIM_REVERSE = {v: k for k, v in PLAN_ANIM_MAP.items()}


def anim_to_plan_name(anim):
    """TextIntro 枚举 → 中间JSON 动画类型字符串（未知则回退 fade_in）。"""
    return PLAN_ANIM_REVERSE.get(anim, "fade_in")


def _suggestion_hint(text, kind):
    """未决策时的提示词（不中断流程），引导主agent在 plan 中补写对应决策。kind: sticker/sfx/effect。

    贴纸/音效/画面特效的语义选择已上移为主agent在 plan 中间JSON 决策（脚本不再做关键词
    硬匹配），此处仅产出自然语言提示，供 agent 在对话中阅读并回填 sticker/音效/effect 轨。
    """
    head = text[:12]
    if kind == "sticker":
        return "建议：根据「%s」语义为主agent挑选 emoji 贴纸（写入 sticker 轨 material 文件名）" % head
    if kind == "effect":
        return "建议：为「%s」配置剪映内置画面特效（写入 effect 轨 material 标识符）" % head
    # 音效
    return "建议：为「%s」配置对应情绪音效（写入音效轨 material 文件名）" % head


def build_plan_json(aligned, dialogs, images, sounds, avatar_files, bg_image,
                    audio_path, total_duration, title=None):
    """自动模式 → 用户给定 schema 的中间 JSON（驱动草稿生成）。

    aligned: [{role,text,audio_start,audio_end,animation,sticker,sfx,scene_effect}]；
    images/sounds: 保留形参以保持调用兼容，但脚本不再用它们做关键词硬匹配
    （贴纸/音效/画面特效的语义选择已上移为主agent在 plan 中决策）。

    【agent 驱动】贴纸/音效/画面特效一律由主agent在对话中按语义挑选，并写入
    aligned 的 sticker / sfx / scene_effect 字段（或直接写 plan 的 sticker/音效/
    effect 轨 material）。脚本无条件尊重传入决策并原样写入中间 JSON，绝不覆盖、
    绝不做关键词兜底匹配。某条消息未预填时 material 留空串并填 hint 提示词，
    供 agent 在对话中阅读并回填。

    语义未命中时 material 置空串并填 hint 提示词（不中断）；
    text/sticker 段都用 audio_start/audio_end（音画同步），且 text/sticker 同窗口；
    每条 text/sticker 段都带 speaker 字段，供后续 calc_bubble_layout 定位。
    硬约束说明：最终草稿的 scale/tx/ty 一律由 calc_sticker_layout 重算，
    JSON 内的 scale/position 仅作展示参考，不参与落草稿。
    """
    # 不直接用文本硬匹配贴纸/音效/特效：尊重 aligned 中已有的决策（由主agent预填），
    # 未预填则留空、由 hint 引导 agent 在对话中补写。
    timeline = build_timeline(aligned, total_duration)   # 生成 display 窗口

    # 重录片段(同文本再次出现, 卡拉OK式独立弹出)自动继承首现条目的贴纸/音效/特效决策,
    # 使重录段与首现段呈现一致(贴纸/特效同样再次触发)。
    _decided = {}
    for _m in timeline["messages"]:
        _key = _m.get("text", "")
        _has = _m.get("sticker") or _m.get("sfx") or _m.get("scene_effect")
        if _has:
            _decided[_key] = (_m.get("sticker"), _m.get("sfx"), _m.get("scene_effect"))
        elif _key in _decided:
            _st, _sf, _ef = _decided[_key]
            _m["sticker"] = _m.get("sticker") or _st
            _m["sfx"] = _m.get("sfx") or _sf
            _m["scene_effect"] = _m.get("scene_effect") or _ef

    meta = {
        "title": title or "AB双人对话短视频",
        "resolution": [CANVAS_WIDTH, CANVAS_HEIGHT],
        "fps": FPS,
        "aspect_ratio": "9:16",
        "bg_color": "#000000",
        "chat_bg_color": "#f5f5f5",
        "output": "output/final_video.mp4",
        "render_quality": "high",
    }

    characters = {}
    for role in ("A", "B"):
        characters[role] = {
            "name": role,
            "avatar": avatar_files.get(role, ""),
            "avatar_size": [AVATAR_DIAMETER, AVATAR_DIAMETER],
            "avatar_border": "circle",
            "bubble_color": "#ffffff" if role == "A" else WECHAT_BUBBLE_COLOR,
            "bubble_shadow": "soft",
            "bubble_border_radius": 18,
            "text_color": "#000000",
            "text_font": "PingFangSC-Regular",
            "text_font_size": 32,
            "side": "left" if role == "A" else "right",
            "position": {"x": 50, "y": 50},
        }

    text_segs = []
    sticker_segs = []
    sfx_segs = []
    effect_segs = []
    for m in timeline["messages"]:
        anim_name = anim_to_plan_name(m.get("animation"))
        text_segs.append({
            "startTime": m["audio_start"],
            "endTime": m["audio_end"],
            "material": m["text"],
            "speaker": m["role"],
            "style": {"font": "PingFangSC-Regular", "size": TEXT_FONT_SIZE,
                      "color": "#000000", "align": "left", "line_height": 1.25},
            "animation": {"type": anim_name, "duration": 0.3, "easing": "ease-out", "delay": 0},
            "position": {"x": 50, "y": 50, "anchor": "left" if m["role"] == "A" else "right"},
        })
        if m.get("sticker"):
            # 读素材真实宽高 → 用与落草稿完全相同的 calc_sticker_layout 计算真实布局，
            # 保证 plan.json 与剪映草稿最终呈现一致（不再写死占位值）
            s_path = _resolve_asset_path(m["sticker"], os.path.dirname(audio_path or "") or ".")
            s_layout = None
            if s_path and os.path.isfile(s_path):
                try:
                    s_layout = calc_sticker_layout(VideoMaterial(s_path))
                except Exception:
                    s_layout = None
            if s_layout:
                s_scale, _, s_tx, s_ty = s_layout
                # position 用百分比（锚点 center）：y_pct = 中心y/画布高*100 = (1+ty)*50
                s_pos = {"x": 50.0, "y": round((1.0 + s_ty) * 50.0, 2), "anchor": "center"}
            else:
                s_scale, s_pos = 0.5, {"x": "50", "y": "30", "anchor": "center"}
            sticker_segs.append({
                "startTime": m["display_start"],
                "endTime": m["display_end"],
                "material": m["sticker"],
                "speaker": m["role"],
                "scale": round(s_scale, 4),   # 真实缩放（1.0=原始大小，与剪映一致）
                "position": s_pos,
                "hint": "",
            })
        else:
            sticker_segs.append({
                "startTime": m["display_start"],
                "endTime": m["display_end"],
                "material": "",
                "speaker": m["role"],
                "scale": 0.5,
                "position": {"x": "50", "y": "30", "anchor": "center"},
                "hint": _suggestion_hint(m["text"], "sticker"),
            })
        if m.get("sfx"):
            sfx_segs.append({
                "startTime": m["audio_start"],
                "endTime": min(m["audio_start"] + SFX_MAX_SEC, total_duration),
                "material": m["sfx"],
                "speaker": m["role"],
                "volume": 0.5,
                "hint": "",
            })
        else:
            sfx_segs.append({
                "startTime": m["audio_start"],
                "endTime": min(m["audio_start"] + SFX_MAX_SEC, total_duration),
                "material": "",
                "speaker": m["role"],
                "volume": 0.5,
                "hint": _suggestion_hint(m["text"], "sfx"),
            })
        # 画面特效：剪映原生 video effect（scene_effect 为 identifier，空串仅占位 hint）
        # 时长封顶 EFFECT_MAX_SEC：气泡连续显示后窗口很长, 特效不宜跟随拉长
        eff = m.get("scene_effect") or ""
        _eff_end = min(m["display_end"], m["display_start"] + EFFECT_MAX_SEC)
        if eff:
            effect_segs.append({
                "startTime": m["display_start"],
                "endTime": _eff_end,
                "material": eff,
                "speaker": m["role"],
                "hint": "",
            })
        else:
            effect_segs.append({
                "startTime": m["display_start"],
                "endTime": _eff_end,
                "material": "",
                "speaker": m["role"],
                "hint": _suggestion_hint(m["text"], "effect"),
            })

    tracks = [
        {"type": "video", "segments": [
            {"startTime": 0.0, "endTime": total_duration,
             "material": bg_image or "", "opacity": 1.0}]},
        {"type": "audio", "segments": [
            {"startTime": 0.0, "endTime": total_duration,
             "material": audio_path, "volume": 1.0,
             "fade_in": 0.0, "fade_out": 0.0}]},
        {"type": "text", "segments": text_segs},
        {"type": "sticker", "segments": sticker_segs},
        {"type": "audio", "segments": sfx_segs},
        {"type": "effect", "segments": effect_segs},
    ]

    return {
        "meta": meta,
        "characters": characters,
        "tracks": tracks,
        "transitions": [{"type": "dissolve", "duration": 0.3, "easing": "ease-in-out"}],
    }


def _resolve_asset_path(path, plan_dir):
    """素材路径统一解析：返回可用的绝对路径（找不到则原样返回）。

    解析顺序：
      1. 绝对路径且存在 → 原样返回；
      2. 相对路径在 plan_dir 下存在 → 返回该绝对路径；
      3. 相对路径（含子目录，如 "新/xxx.gif"）在 skill 内置贴图库 assets/emojis/ 下存在
         → 返回该绝对路径；
      4. 仅文件名（如 sad_01.png）在 skill 内置音效库 assets/sfx/ 下存在
         → 返回该绝对路径；
      5. 均找不到 → 返回原 path，由调用方决定报 hint（不在此抛错）。
    """
    if not path or not isinstance(path, str):
        return path
    if os.path.isabs(path):
        if os.path.isfile(path):
            return path
        return path
    if plan_dir:
        abs_path = os.path.normpath(os.path.join(plan_dir, path))
        if os.path.isfile(abs_path):
            return abs_path
    # 尝试 skill 内置贴图库 assets/emojis/（支持子目录，如 "新/xxx.gif"）
    if os.path.isdir(DEFAULT_STICKER_DIR):
        builtin = os.path.normpath(os.path.join(DEFAULT_STICKER_DIR, path))
        if os.path.isfile(builtin):
            return builtin
    # 尝试 skill 内置音效库 assets/sfx/（支持子目录）
    if os.path.isdir(DEFAULT_SOUND_DIR):
        builtin = os.path.normpath(os.path.join(DEFAULT_SOUND_DIR, path))
        if os.path.isfile(builtin):
            return builtin
    return path


def load_plan_json(path):
    """读取并校验中间 JSON，返回 dict；结构异常给出明确中文错误。

    校验: 文件存在且 .json；顶层 dict 含 meta/tracks；tracks 非空；
          至少一个 audio 段（音频时长来源）。
    素材解析统一走 _resolve_asset_path（绝对路径 / 相对 JSON 目录 /
    skill 内置 assets/emojis 兜底；text 段 material 是文本不解析）。
    """
    if not os.path.isfile(path) or not path.lower().endswith(".json"):
        fail(ERR_BAD_PLAN_PATH)
    try:
        with open(path, "r", encoding="utf-8") as fp:
            plan = json.load(fp)
    except Exception:
        fail(ERR_BAD_PLAN)
    if not isinstance(plan, dict) or not isinstance(plan.get("meta"), dict) \
            or not isinstance(plan.get("tracks"), list) or not plan["tracks"]:
        fail(ERR_BAD_PLAN)
    has_audio = any(t.get("type") == "audio" and t.get("segments")
                    for t in plan["tracks"])
    if not has_audio:
        fail(ERR_PLAN_ALIGN)

    # 素材路径统一解析（相对中间JSON所在目录 / skill 内置贴图库兜底）
    plan_dir = os.path.dirname(os.path.abspath(path))

    for tr in plan["tracks"]:
        if tr.get("type") in ("video", "sticker", "audio"):
            for seg in tr.get("segments", []):
                if isinstance(seg, dict) and seg.get("material"):
                    raw = seg["material"]
                    seg["_raw_material"] = raw   # 留原名，规范化写回时还原（避免写绝对路径）
                    seg["material"] = _resolve_asset_path(raw, plan_dir)
    for role in plan.get("characters", {}).values():
        if isinstance(role, dict) and role.get("avatar"):
            role["avatar"] = _resolve_asset_path(role["avatar"], plan_dir)
    return plan


def plan_total_us(plan):
    """从中间JSON audio 段推断音频总时长（微秒）。"""
    total = 0.0
    for tr in plan["tracks"]:
        if tr.get("type") != "audio":
            continue
        for seg in tr.get("segments", []):
            if isinstance(seg, dict):
                total = max(total, float(seg.get("endTime", 0.0)))
    return int(round(total * 1e6))


def plan_voice_path(plan):
    """取中间JSON第一个 audio 段（完整配音）的素材路径，无则返回空串。"""
    for tr in plan["tracks"]:
        if tr.get("type") != "audio":
            continue
        for seg in tr.get("segments", []):
            if isinstance(seg, dict) and seg.get("material"):
                return seg["material"]
    return ""


def fill_draft_from_plan(sf, plan, audio_path, total_us):
    """从中间 JSON 填充剪映草稿（微信样式逻辑与 fill_draft 完全一致）。

    硬约束（中间JSON不得覆盖）:
      1) 音画同步: 文本气泡 / 贴图 / 头像严格对齐人声时间窗 audio_start~audio_end（无尾、无顺延）;
      2) 贴图尺寸位置: 一律由 calc_sticker_layout 按 756px 目标宽/动态缩放/
         气泡中心钳制重算，无视 JSON 的 scale/position 字段;
    文本/贴图段按序一一配对（第 i 条 text ↔ 第 i 条 sticker，speaker 取各自字段）;
    material 为空仅含 hint 的贴图/音效段跳过并打印提示。
    """
    # ---- 建全部轨道（图层顺序同 fill_draft）----
    sf.add_track(TrackType.video, "背景轨")                       # render 0 最底层
    sf.add_track(TrackType.audio, "音频轨道")                     # render 0
    sf.add_track(TrackType.video, "贴图轨", relative_index=5)     # render 5
    sf.add_track(TrackType.video, "头像轨", relative_index=6)     # render 6
    sf.add_track(TrackType.text, "气泡文本轨", relative_index=1)  # render 15001
    sf.add_track(TrackType.audio, "音效轨", relative_index=1)     # render 1

    # ---- 0) 背景轨: 中间JSON video 段第一段非空素材铺满画布 ----
    bg_image = ""
    for tr in plan["tracks"]:
        if tr.get("type") == "video":
            for seg in tr.get("segments", []):
                if isinstance(seg, dict) and seg.get("material"):
                    bg_image = seg["material"]
                    break
            break
    if bg_image:
        bg_mat = VideoMaterial(bg_image)
        raw_w, raw_h = float(bg_mat.width), float(bg_mat.height)
        bg_disp_h = CANVAS_HEIGHT
        bg_disp_w = int(bg_disp_h * raw_w / raw_h)
        if bg_disp_w < CANVAS_WIDTH:
            bg_disp_w = CANVAS_WIDTH
            bg_disp_h = int(bg_disp_w * raw_h / raw_w)
        bg_scale_x = bg_disp_w / CANVAS_WIDTH
        bg_scale_y = bg_scale_x  # 等比 cover，防止变形
        bg_seg = VideoSegment(bg_mat, Timerange(0, int(total_us)),
                              clip_settings=ClipSettings(scale_x=bg_scale_x, scale_y=bg_scale_y))
        bg_seg.uniform_scale = False
        sf.add_segment(bg_seg, track_name="背景轨")

    # ---- 1) 音频轨: 完整配音铺整曲（audio_path 取中间JSON第一个 audio 段素材）----
    if audio_path and os.path.isfile(audio_path):
        audio_mat = AudioMaterial(audio_path)
        sf.add_segment(AudioSegment(audio_mat, Timerange(0, int(total_us))),
                       track_name="音频轨道")
    else:
        print("提示：中间JSON缺少完整配音音频段，跳过主音频轨")

    # ---- 2) 文本轨 + 3) 贴图轨: 按序配对，微信气泡/动态贴图 ----
    text_tr = next((t for t in plan["tracks"] if t.get("type") == "text"), None)
    sticker_tr = next((t for t in plan["tracks"] if t.get("type") == "sticker"), None)
    text_segs = text_tr.get("segments", []) if text_tr else []
    sticker_segs = sticker_tr.get("segments", []) if sticker_tr else []

    # 唱词反复/重录会生成互相重叠的时间窗(如第6句重录段与第7句主段交叠) → 区间分层,
    # 重叠消息放独立轨道(剪映同一轨道不允许片段重叠)。
    text_windows = []
    for i, tseg in enumerate(text_segs):
        if not isinstance(tseg, dict) or not tseg.get("material"):
            text_windows.append((0.0, 0.1))
            continue
        ds = float(tseg.get("startTime", 0.0))
        de = float(tseg.get("endTime", 0.0))
        if i > 0 and isinstance(text_segs[i - 1], dict):
            prev_start = float(text_segs[i - 1].get("startTime", 0.0))
            if ds < prev_start:
                ds = prev_start
        if de <= ds:
            de = ds + 0.3
        text_windows.append((ds, de))
    t_layers = _assign_layers(text_windows)
    _n = (max(t_layers) + 1) if t_layers else 1
    for _li in range(1, _n):
        sf.add_track(TrackType.text, _layer_track("气泡文本轨", _li))
        sf.add_track(TrackType.video, _layer_track("贴图轨", _li))
        sf.add_track(TrackType.video, _layer_track("头像轨", _li))
        sf.add_track(TrackType.effect, _layer_track("EffectTrack", _li))

    prev_anim = None
    disp_windows = {}      # 消息序号 → (start_us, dur_us)，供头像轨复用同一显示窗口
    for i, tseg in enumerate(text_segs):
        if not isinstance(tseg, dict) or not tseg.get("material"):
            continue
        role = str(tseg.get("speaker", "A")).strip().upper()
        if role not in ("A", "B"):
            role = "A"
        text = tseg["material"]
        # 音画同步硬约束: 气泡严格对齐人声时间窗 audio_start~audio_end（无尾、无顺延），
        # 避免短句时上一条 0.4s 尾巴把下一条气泡推后造成音画脱节。SFX 同样用 audio_start。
        # 唱词反复/重录会把窗口扩展到相邻句(对唱场景允许气泡重叠), 重叠消息由分层轨承载。
        ds, de = text_windows[i]
        start_us = int(round(ds * 1e6))
        dur_us = int(round((de - ds) * 1e6))
        disp_windows[i] = (start_us, dur_us)

        bg_w, bg_h, t_x, line_max, center_px = calc_bubble_layout(role, text)
        # 聊天框样式：启用内置"会话"气泡时由气泡提供背景（关闭 TextBackground/描边，文字改黑）；
        # 否则维持原微信绿底白字样式。
        if CHAT_BUBBLE_ENABLED:
            text_color = (0, 0, 0)
            background = None
            border = None
        else:
            text_color = WECHAT_TEXT_COLOR
            background = TextBackground(color=WECHAT_BUBBLE_COLOR, style=1, alpha=1.0,
                                        round_radius=BUBBLE_ROUND_RADIUS,
                                        height=bg_h, width=bg_w,
                                        horizontal_offset=0.5, vertical_offset=0.5)
            border = TextBorder(color=(0, 0, 0), width=20.0)
        style = TextStyle(size=TEXT_FONT_SIZE, bold=True, color=text_color,
                          align=TEXT_ALIGN[role], auto_wrapping=True,
                          max_line_width=line_max)
        clip = ClipSettings(transform_x=t_x, transform_y=MSG_CENTER_Y)
        seg = TextSegment(text, Timerange(start_us, dur_us),
                          style=style, border=border, background=background,
                          clip_settings=clip)
        # 动画: JSON animation.type 优先, 否则按文本特征规则选
        anim = None
        anim_dur = None
        if isinstance(tseg.get("animation"), dict):
            a_type = tseg["animation"].get("type")
            if a_type in PLAN_ANIM_MAP:
                anim = PLAN_ANIM_MAP[a_type]
                a_dur = tseg["animation"].get("duration")
                if isinstance(a_dur, (int, float)) and a_dur > 0:
                    anim_dur = int(a_dur * 1e6)
        if anim is None:
            anim = pick_animation(role, text, prev_anim)
        seg.add_animation(anim, duration=anim_dur)
        prev_anim = anim
        sf.add_segment(seg, track_name=_layer_track("气泡文本轨", t_layers[i]))

        # ---- 配对贴图（第 i 条 sticker ↔ 第 i 条 text）----
        if i < len(sticker_segs) and isinstance(sticker_segs[i], dict):
            sseg = sticker_segs[i]
            s_path = sseg.get("material", "")
            if s_path and os.path.isfile(s_path):
                smat = VideoMaterial(s_path)
                scale_x, scale_y, tx, ty = calc_sticker_layout(smat)
                sclip = ClipSettings(scale_x=scale_x, scale_y=scale_y,
                                     transform_x=tx, transform_y=ty)
                # GIF 等短素材: source 截取不超过素材时长(慢放铺满窗口)
                ssrc_us = int(min(smat.duration, dur_us))
                sticker_seg = VideoSegment(smat, Timerange(start_us, dur_us),
                                           source_timerange=Timerange(0, ssrc_us),
                                           clip_settings=sclip)
                sticker_seg.uniform_scale = False
                sf.add_segment(sticker_seg,
                               track_name=_layer_track("贴图轨", t_layers[i]))
            else:
                hint = sseg.get("hint", "")
                print("提示：第%d条消息未匹配贴图，hint=%s"
                      % (i + 1, hint or "无"))

    # ---- 4) 头像轨: 中间JSON characters.avatar（A 左 / B 右，圆形）----
    # 显示窗口复用文本轨计算结果（disp_windows），保证与气泡同步且不重叠
    characters = plan.get("characters", {}) or {}
    for i, tseg in enumerate(text_segs):
        if not isinstance(tseg, dict) or not tseg.get("material"):
            continue
        raw_role = str(tseg.get("speaker", "A")).strip()
        # 优先用原始角色名查找，找不到再用规范化 A/B
        role = raw_role.upper() if raw_role.upper() in ("A", "B") else None
        if role:
            avatar = characters.get(role, {}).get("avatar", "")
        else:
            # 中文角色名：先用原始名查，再用规范化名查
            avatar = characters.get(raw_role, {}).get("avatar", "")
            if not avatar:
                avatar = characters.get("A", {}).get("avatar", "") if raw_role == characters.get("A", {}).get("name", "") else ""
            if not avatar:
                avatar = characters.get("B", {}).get("avatar", "") if raw_role == characters.get("B", {}).get("name", "") else ""
            role = "A"  # 默认左侧
        if not avatar or not os.path.isfile(avatar):
            continue
        amat = VideoMaterial(avatar)
        # JianYing 语义: scale = 显示宽/舞台宽, 100%=1080px
        scale = AVATAR_DIAMETER / float(CANVAS_WIDTH)
        if role == "A":
            ax = (AVATAR_LEFT_PX + AVATAR_DIAMETER / 2 - CANVAS_WIDTH / 2) / (CANVAS_WIDTH / 2)
        else:
            ax = (AVATAR_RIGHT_PX - AVATAR_DIAMETER / 2 - CANVAS_WIDTH / 2) / (CANVAS_WIDTH / 2)
        if i not in disp_windows:
            continue
        start_us, dur_us = disp_windows[i]
        clip = ClipSettings(scale_x=scale, scale_y=scale,
                            transform_x=round(ax, 4), transform_y=AVATAR_TRANSFORM_Y)
        seg = VideoSegment(amat, Timerange(start_us, dur_us), clip_settings=clip)
        seg.uniform_scale = False
        # 矩形蒙版(圆角50)：矩形头像框，非圆形
        seg.add_mask(MaskType.矩形, size=1.0, rect_width=1.0,
                     round_corner=AVATAR_MASK_ROUND_CORNER)
        sf.add_segment(seg, track_name=_layer_track("头像轨", t_layers[i]))

    # ---- 5) 音效轨/BGM: 中间JSON 其余 audio 段按 startTime/endTime 放置 ----
    # 注意：跳过完整配音段（与 audio_path 同一文件），避免重复铺整曲
    voice_norm = os.path.normpath(os.path.abspath(audio_path)) if audio_path else ""
    last_end_us = 0
    for tr in plan["tracks"]:
        if tr.get("type") != "audio":
            continue
        for sseg in tr.get("segments", []):
            if not isinstance(sseg, dict):
                continue
            s_path = sseg.get("material", "")
            if not s_path or not os.path.isfile(s_path):
                if sseg.get("hint"):
                    print("提示：未匹配音效，hint=%s" % sseg["hint"])
                continue
            if voice_norm and os.path.normpath(os.path.abspath(s_path)) == voice_norm:
                continue   # 完整配音已在音频轨铺整曲，不重复放置
            start_us = int(round(float(sseg.get("startTime", 0.0)) * 1e6))
            end_us = int(round(float(sseg.get("endTime", 0.0)) * 1e6))
            start_us = max(start_us, last_end_us)
            dur_us = end_us - start_us
            if dur_us <= 0:
                continue
            smat = AudioMaterial(s_path)
            if dur_us > smat.duration:      # 素材不足时按素材时长截断
                dur_us = int(smat.duration)
            vol = sseg.get("volume", 1.0)
            try:
                vol = float(vol)
            except (TypeError, ValueError):
                vol = 1.0
            seg = AudioSegment(smat, Timerange(start_us, dur_us), volume=vol)
            # 淡入淡出（JSON fade_in/fade_out 秒 → 微秒）
            try:
                f_in = float(sseg.get("fade_in", 0.0))
            except (TypeError, ValueError):
                f_in = 0.0
            try:
                f_out = float(sseg.get("fade_out", 0.0))
            except (TypeError, ValueError):
                f_out = 0.0
            if f_in > 0 or f_out > 0:
                seg.add_fade(int(f_in * 1e6), int(f_out * 1e6))
            sf.add_segment(seg, track_name="音效轨")
            last_end_us = max(last_end_us, start_us + dur_us)

    # ---- 6) 画面特效轨(剪映原生 video effect): 中间JSON effect 段按 startTime/endTime 放置 ----
    # 特效时长封顶 EFFECT_MAX_SEC 后可能与相邻特效重叠(文本窗无缝≠特效窗无缝), 故按
    # 特效实际窗口重新分层(_assign_layers), 重叠特效放独立 EffectTrack 层。
    eff_tr = next((t for t in plan["tracks"] if t.get("type") == "effect"), None)
    if eff_tr and NATIVE_EFFECTS_ENABLED:
        _eff_win = []
        _eff_objs = []                 # (eff_type, start_us, dur_us)
        for eseg in eff_tr.get("segments", []):
            if not isinstance(eseg, dict) or not eseg.get("material"):
                if isinstance(eseg, dict) and eseg.get("hint"):
                    print("提示：未匹配画面特效，hint=%s" % eseg["hint"])
                continue
            eff_type = _resolve_video_effect(eseg["material"])
            if not eff_type:
                print("提示：画面特效「%s」未在剪映模板匹配，跳过" % eseg["material"])
                continue
            es = float(eseg.get("startTime", 0.0))
            ee = float(eseg.get("endTime", 0.0))
            dur = min(max((ee - es), 0.3), EFFECT_MAX_SEC)
            _eff_win.append((es, es + dur))
            _eff_objs.append((eff_type, int(round(es * 1e6)), int(round(dur * 1e6))))
        if _eff_objs:
            _eff_layers = _assign_layers(_eff_win)
            for (_eff_type, _start_us, _dur_us), _ly in zip(_eff_objs, _eff_layers):
                _tr = _layer_track("EffectTrack", _ly)
                try:
                    sf.add_track(TrackType.effect, _tr)
                except Exception:
                    pass
                # 用 add_effect（而非 add_segment）才会把特效素材注册到 materials.video_effects，
                # 否则剪映有轨道无素材 → 渲染不出
                try:
                    sf.add_effect(_eff_type, Timerange(_start_us, _dur_us), track_name=_tr)
                except Exception as e:
                    print("提示：画面特效「%s」插入异常: %s" % (eseg["material"], e))


# ---------------------------------------------------------------- 入口 ----
def parse_args():
    p = argparse.ArgumentParser(description="AB双人对话短视频 → 剪映5.9草稿生成器")
    p.add_argument("--dialog-json", dest="dialog_json_path", default=None,
                   help="AB对话文本JSON文件路径（必填）")
    p.add_argument("--audio-mp3", dest="full_audio_mp3_path", default=None,
                   help="完整配音MP3文件路径（必填）")
    p.add_argument("--sticker-dir", dest="sticker_lib_dir",
                   default=DEFAULT_STICKER_DIR,
                   help="贴图素材库目录（可选，缺省用skill内置贴图库）")
    p.add_argument("--sound-dir", dest="sound_lib_dir",
                   default=DEFAULT_SOUND_DIR,
                   help="氛围音效素材库目录（可选，缺省用skill内置音效库）")
    p.add_argument("--template-dir", dest="blank_clip_template_dir",
                   default=DEFAULT_TEMPLATE_DIR,
                   help="剪映5.9空白草稿模板目录（可选，缺省用skill内置模板）")
    p.add_argument("--output-draft", dest="output_draft_dir",
                   default=default_draft_root(),
                   help="草稿输出目录（可选，缺省输出到剪映默认草稿目录）")
    p.add_argument("--plan-json", dest="plan_json_path", default=None,
                   help="中间JSON驱动模式：直接读取中间JSON生成草稿（此时 --dialog-json/--audio-mp3 可选，优先于自动模式）")
    p.add_argument("--export-plan", dest="export_plan_path", default=None,
                   help="自动模式下额外把中间JSON写入该路径；仅给此参数且无 --dialog-json/--audio-mp3 时给出明确错误")
    p.add_argument("positional", nargs="*", help=argparse.SUPPRESS)
    a = p.parse_args()

    # 位置参数按契约顺序兜底（output_draft_dir 有默认值时，仅当用户未显式
    # 用 --output-draft 命名参数且位置参数传满 6 个才用位置参数覆盖）
    order = ["dialog_json_path", "full_audio_mp3_path", "sticker_lib_dir",
             "sound_lib_dir", "blank_clip_template_dir", "output_draft_dir"]
    output_draft_named = any(x == "--output-draft" or x.startswith("--output-draft=")
                             for x in sys.argv[1:])
    for i, name in enumerate(order):
        if i >= len(a.positional):
            continue
        if not getattr(a, name):
            setattr(a, name, a.positional[i])
        elif name == "output_draft_dir" and not output_draft_named:
            setattr(a, name, a.positional[i])
    return a


def normalize_plan_sticker_layout(plan, plan_path):
    """规范化中间JSON的 sticker 段：读素材真实宽高，按 calc_sticker_layout 规则
    重算 scale/position（与落草稿完全一致），写回 JSON 文件。

    - scale: 剪映贴纸缩放比例（1.0=原始大小，建议 0.1~5.0），全宽铺满但受
      MAX_STICKER_SCALE 上限约束，超出时居中显示（小图不过度放大防模糊）；
    - position: 画布百分比坐标，anchor=center，y 由贴底 5% 布局换算；
    - material 用 _raw_material 原名写回（避免落绝对路径）。
    """
    sticker_trs = [tr for tr in plan.get("tracks", []) if tr.get("type") == "sticker"]
    updated = 0
    touched = []  # (seg,) 需要临时还原 material 写回文件的段
    for tr in sticker_trs:
        for seg in tr.get("segments", []):
            if not isinstance(seg, dict) or not seg.get("material"):
                continue
            s_path = seg["material"]          # load_plan_json 已解析为绝对路径
            if not os.path.isfile(s_path):
                continue
            try:
                s_scale, _, s_tx, s_ty = calc_sticker_layout(VideoMaterial(s_path))
            except Exception:
                continue
            seg["scale"] = round(s_scale, 4)
            seg["position"] = {
                "x": "50",                                  # 水平居中
                "y": str(round((1.0 + s_ty) * 50.0, 2)),    # center 锚点换算
                "anchor": "center",
            }
            touched.append(seg)
            updated += 1
    if updated:
        # 写回文件用原始素材名；内存中的绝对路径保留（供 fill_draft_from_plan 使用）
        backups = []
        for seg in touched:
            if seg.get("_raw_material"):
                backups.append((seg, seg["material"]))
                seg["material"] = seg["_raw_material"]
        try:
            with open(plan_path, "w", encoding="utf-8") as fp:
                json.dump(plan, fp, ensure_ascii=False, indent=2)
            print("已规范化 %d 个贴图段的 scale/position（读取素材宽高重算，与剪映草稿一致）" % updated)
        except Exception as e:
            print("警告：写回中间JSON失败：%s" % e)
        for seg, abs_path in backups:
            seg["material"] = abs_path
        for seg in touched:
            seg.pop("_raw_material", None)
    return plan


def _main_plan(a):
    """中间JSON驱动模式: 读取 plan → [可选ASR对齐] → 校验 → 填充草稿。

    若同时提供 --dialog-json 和 --audio-mp3，则先做 ASR 对齐，再用对齐后的时间戳
    更新 plan 的 text 段（保留 sticker/sfx 的语义决策）。
    """
    plan = load_plan_json(a.plan_json_path)

    # ---- 可选: ASR 对齐覆盖时间戳 ----
    if a.dialog_json_path and a.full_audio_mp3_path:
        print("检测到 --dialog-json + --audio-mp3，执行 ASR 对齐...")
        dialogs, role_map = load_dialogs(a.dialog_json_path)
        audio_mat = AudioMaterial(a.full_audio_mp3_path)
        total_duration = int(audio_mat.duration) / 1e6
        asr_segs = run_asr(a.full_audio_mp3_path)
        aligned = asr_align(dialogs, asr_segs, total_duration)
        # 用 ASR 对齐结果更新 plan 的 text/sticker/sfx 段时间戳
        text_segs = []
        sticker_segs = []
        sfx_segs = []
        voice_path_norm = os.path.normpath(os.path.abspath(a.full_audio_mp3_path))
        for tr in plan.get("tracks", []):
            if tr.get("type") == "text":
                text_segs = tr.get("segments", [])
            elif tr.get("type") == "sticker":
                sticker_segs = tr.get("segments", [])
            elif tr.get("type") == "audio":
                for seg in tr.get("segments", []):
                    s_path = seg.get("material", "")
                    # 排除主配音段（整段铺底的音频），其余均为音效
                    if s_path and os.path.isfile(str(s_path)):
                        if os.path.normpath(os.path.abspath(s_path)) == voice_path_norm:
                            continue
                    sfx_segs.append(seg)
        # 逐条更新：text 用 audio_start/audio_end；sticker 用 display 窗口；sfx 用 audio 起点+上限
        for i, a_info in enumerate(aligned):
            new_start = float(a_info["audio_start"])
            new_end = float(a_info["audio_end"])
            disp_start = new_start
            disp_end = new_end
            sfx_end = min(new_start + SFX_MAX_SEC, float(total_duration))
            if i < len(text_segs):
                text_segs[i]["startTime"] = new_start
                text_segs[i]["endTime"] = new_end
                text_segs[i]["speaker"] = a_info["role"]
            if i < len(sticker_segs):
                sticker_segs[i]["startTime"] = disp_start
                sticker_segs[i]["endTime"] = disp_end
                sticker_segs[i]["speaker"] = a_info["role"]
            if i < len(sfx_segs):
                sfx_segs[i]["startTime"] = new_start
                sfx_segs[i]["endTime"] = sfx_end
                sfx_segs[i]["speaker"] = a_info["role"]
        # 更新 plan 中主配音 audio 段的 endTime 为实际音频时长
        for tr in plan.get("tracks", []):
            if tr.get("type") == "audio":
                for seg in tr.get("segments", []):
                    s_path = seg.get("material", "")
                    if s_path and os.path.isfile(str(s_path)):
                        if os.path.normpath(os.path.abspath(s_path)) == voice_path_norm:
                            seg["endTime"] = float(total_duration)
                            break
                break
        # 加载头像（支持中文角色名）
        characters = plan.get("characters", {})
        for orig_name, ab_role in role_map.items():
            if orig_name not in ("A", "B") and ab_role in characters:
                av = find_avatar_image(a.dialog_json_path, orig_name)
                if av:
                    characters[ab_role]["avatar"] = av
        print("ASR 对齐完成，已更新 %d 条时间戳" % len(aligned))

    plan = normalize_plan_sticker_layout(plan, a.plan_json_path)

    if not os.path.isdir(a.blank_clip_template_dir) or not (
            os.path.isfile(os.path.join(a.blank_clip_template_dir, "draft_content.json"))
            or os.path.isfile(os.path.join(a.blank_clip_template_dir, "draft_info.json"))
            or os.path.isfile(os.path.join(a.blank_clip_template_dir, "draft_meta_info.json"))):
        fail(ERR_BAD_TEMPLATE)                                    # 校验6

    try:
        os.makedirs(a.output_draft_dir, exist_ok=True)
    except Exception:
        fail(ERR_OUTPUT_DIR)                                      # 校验7

    try:
        total_us = plan_total_us(plan)
        if total_us <= 0:
            fail(ERR_PLAN_ALIGN)
        audio_path = plan_voice_path(plan)   # 完整配音 = 中间JSON第一个 audio 段素材

        draft_name = "ab_dialog_" + time.strftime("%Y%m%d_%H%M%S")
        sf = init_script(a.blank_clip_template_dir, draft_name)
        fill_draft_from_plan(sf, plan, audio_path, total_us)
        out_draft_dir = save_draft(sf, a.blank_clip_template_dir,
                                   a.output_draft_dir, draft_name)
        # 气泡颜色(A=白/对方, B=绿/自己)按 transform.x 符号判定: 重录重叠段会分到
        # 独立文本轨(段序与 plan 不再一致), 而气泡宽已封顶 420px 不会越过画布中线,
        # 故 A(左) 恒为负 tx、B(右) 恒为正 tx, 符号判定可靠。
        _inject_chat_bubbles_ab(os.path.join(out_draft_dir, "draft_info.json"), roles=None)

        print("执行完成！剪映草稿输出路径：%s" % a.output_draft_dir)
        print("草稿目录：%s" % out_draft_dir)
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        fail("错误：%s" % e)


def main():
    a = parse_args()

    # ---- 模式选择: 中间JSON驱动(plan-json) 优先于 自动模式(dialog+audio) ----
    if a.plan_json_path:
        _main_plan(a)
        return

    # ---- 前置校验 1-8 (与契约逐字一致) ----
    if not a.dialog_json_path or not a.full_audio_mp3_path:
        if a.export_plan_path:
            fail("错误：导出中间JSON需要 --dialog-json 与 --audio-mp3 输入（自动模式），或提供 --plan-json 走中间JSON模式")
        fail(ERR_MISSING_PARAMS)                                  # 校验1

    if not os.path.isfile(a.dialog_json_path) or not a.dialog_json_path.lower().endswith(".json"):
        fail(ERR_BAD_DIALOG_PATH)                                 # 校验2

    if not os.path.isfile(a.full_audio_mp3_path) or not a.full_audio_mp3_path.lower().endswith(".mp3"):
        fail(ERR_BAD_AUDIO_PATH)                                  # 校验3

    if not os.path.isdir(a.sticker_lib_dir):
        fail(ERR_NO_STICKER_DIR)                                  # 校验4

    if not os.path.isdir(a.sound_lib_dir):
        fail(ERR_NO_SOUND_DIR)                                    # 校验5

    if not os.path.isdir(a.blank_clip_template_dir) or not (
            os.path.isfile(os.path.join(a.blank_clip_template_dir, "draft_content.json"))
            or os.path.isfile(os.path.join(a.blank_clip_template_dir, "draft_info.json"))
            or os.path.isfile(os.path.join(a.blank_clip_template_dir, "draft_meta_info.json"))):
        fail(ERR_BAD_TEMPLATE)                                    # 校验6

    try:
        os.makedirs(a.output_draft_dir, exist_ok=True)
    except Exception:
        fail(ERR_OUTPUT_DIR)                                      # 校验7

    dialogs, role_map = load_dialogs(a.dialog_json_path)            # 校验8(结构)

    # ---- 主流程: 素材加载 → ASR对齐 → 编导 → 适配 → 填充 ----
    try:
        # Step1 素材加载
        images = scan_files(a.sticker_lib_dir, IMAGE_EXTS)
        sounds = scan_files(a.sound_lib_dir, AUDIO_EXTS)
        audio_mat = AudioMaterial(a.full_audio_mp3_path)
        total_us = int(audio_mat.duration)
        total_duration = total_us / 1e6
        # 头像/背景图: 默认读取 dialog.json 同目录的 A.png/B.png/BG.png(不存在则兜底)
        # 同时尝试中文角色名头像（如 贾总.png/王工.png）
        avatar_files = {}
        for role in ("A", "B"):
            av = find_avatar_image(a.dialog_json_path, role)
            if av:
                avatar_files[role] = av
        # 尝试用中文角色名加载头像（覆盖默认 A/B 头像）
        for orig_name, ab_role in role_map.items():
            if orig_name not in ("A", "B"):
                av = find_avatar_image(a.dialog_json_path, orig_name)
                if av:
                    avatar_files[ab_role] = av
        bg_image = find_background_image(a.dialog_json_path)
        if avatar_files or bg_image:
            print("已加载同目录素材: 头像=%s 背景=%s"
                  % (avatar_files or "无", bg_image or "无"))

        # Step2 ASR 对齐
        print("正在进行语音识别(ASR)对齐, 请稍候...")
        asr_segs = run_asr(a.full_audio_mp3_path)
        aligned = asr_align(dialogs, asr_segs, total_duration)

        # Step3 AI编导(规则引擎)：仅做入场动画决策(基于句式的轻量规则)；
        # 贴纸/音效/画面特效的语义选择已上移为主agent在 plan 中决策，脚本不再硬匹配。
        prev_anim = None
        for m in aligned:
            m["animation"] = pick_animation(m["role"], m["text"], prev_anim)
            prev_anim = m["animation"]

        # Step4 数据适配
        timeline = build_timeline(aligned, total_duration)

        # Step4.5 构建中间JSON（自动模式默认导出; 亦可作为 plan-json 模式输入）
        plan = build_plan_json(aligned, dialogs, images, sounds, avatar_files,
                               bg_image, a.full_audio_mp3_path, total_duration)

        # Step5 草稿填充
        draft_name = "ab_dialog_" + time.strftime("%Y%m%d_%H%M%S")
        sf = init_script(a.blank_clip_template_dir, draft_name)
        fill_draft(sf, timeline, a.full_audio_mp3_path, total_us,
                   avatar_files=avatar_files, bg_image=bg_image)
        out_draft_dir = save_draft(sf, a.blank_clip_template_dir,
                                   a.output_draft_dir, draft_name)
        # 气泡颜色(A=白/对方, B=绿/自己)按 transform.x 符号判定: 重录重叠段会分到
        # 独立文本轨(段序与 plan 不再一致), 而气泡宽已封顶 420px 不会越过画布中线,
        # 故 A(左) 恒为负 tx、B(右) 恒为正 tx, 符号判定可靠。
        _inject_chat_bubbles_ab(os.path.join(out_draft_dir, "draft_info.json"), roles=None)

        # 自动模式下默认导出中间JSON到草稿同目录; --export-plan 额外导出到指定路径
        plan_out = os.path.join(out_draft_dir, "plan_%s.json" % draft_name)
        with open(plan_out, "w", encoding="utf-8") as fp:
            json.dump(plan, fp, ensure_ascii=False, indent=2)
        print("中间JSON已导出：%s" % plan_out)
        if a.export_plan_path:
            with open(a.export_plan_path, "w", encoding="utf-8") as fp:
                json.dump(plan, fp, ensure_ascii=False, indent=2)
            print("中间JSON已导出：%s" % a.export_plan_path)

        # 成功输出
        print("执行完成！剪映草稿输出路径：%s" % a.output_draft_dir)
        print("草稿目录：%s" % out_draft_dir)
    except SystemExit:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        fail("错误：%s" % e)


if __name__ == "__main__":
    main()
