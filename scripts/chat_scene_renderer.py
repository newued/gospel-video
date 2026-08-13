# -*- coding: utf-8 -*-
"""聊天消息渲染器 v4 — 仿微信单条消息截图风格。

设计要点：
  - 每条消息像微信聊天截图一样，居中出现
  - 左角色：头像在左，白色气泡在右，角色名在气泡上方靠左
  - 右角色：头像在右，绿色气泡在左，角色名在气泡上方靠右
  - 旁白：居中半透明黑底白字胶囊
  - 贴纸/表情包在气泡正下方居中弹出
  - 背景：默认微信灰底色 #ededed，支持自定义背景图 bg.png/bg.jpg
  - 头像：默认圆形首字头像，支持 A.png/B.png 自定义
  - 9种入场动画交替使用，旧消息上移淡出
"""
import os
import random
import shutil
import sys
import tempfile

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))
try:
    from media_utils import get_image_size
except ImportError:
    get_image_size = None


def _abs_url(path: str) -> str | None:
    """本地路径转 file:// URL。"""
    if not path:
        return None
    p = path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)
    if os.path.exists(p):
        return "file:///" + p.replace("\\", "/")
    return None


# 消息入场动画（9种交替）
MSG_ENTRY_ANIMS = [
    "msgPop", "msgSlideUp", "msgSlideLeft", "msgSlideRight",
    "msgZoom", "msgBounce", "msgSwing", "msgDrop", "msgFlip"
]

# 贴纸入场动画
STICKER_ANIMS = [
    "stickerBounce", "stickerUp", "stickerZoom", "stickerWobble", "stickerPop"
]

# 默认头像颜色（根据角色名hash分配）
DEFAULT_AVATAR_COLORS = [
    "#4a9eff", "#ff6b6b", "#51cf66", "#cc5de8",
    "#ff922b", "#22b8cf", "#f06595", "#94d82d"
]


def _pick_avatar_color(role: str) -> str:
    """根据角色名稳定分配头像颜色。"""
    if not role:
        return DEFAULT_AVATAR_COLORS[0]
    h = 0
    for ch in role:
        h = (h * 31 + ord(ch)) % len(DEFAULT_AVATAR_COLORS)
    return DEFAULT_AVATAR_COLORS[h]


def build_chat_html(script: dict, msg_timings: list | None = None,
                    speedup: float = 1.0, total_duration: float = None,
                    asset_dir: str = None) -> str:
    """生成仿微信单条消息截图风格 HTML。

    Args:
        script: {"title": str, "messages": [...], "bg": str(可选), "avatars": {role: path}}
        msg_timings: [(start_s, end_s), ...]
        speedup: 倍速
        total_duration: 视频总时长
        asset_dir: 素材目录（用于自动查找 bg.png/A.png/B.png）
    """
    messages = script.get("messages", [])
    title = script.get("title", "")
    timings = msg_timings or [1.2 * i for i in range(len(messages))]

    def _t0(t):
        return t[0] if isinstance(t, (tuple, list)) else t

    def _t1(t):
        return t[1] if isinstance(t, (tuple, list)) else t

    def _tslot(t, slot, default=None):
        """兼容扩展：timings 项可携带 (start, end, ...) 之外的决策字段。
        第3个=side，第4个=animation，第5个=overlay_sticker。"""
        if isinstance(t, (tuple, list)) and len(t) > slot:
            return t[slot]
        return default

    # ---- 确定左右角色分配 ----
    # 第一个出现的非旁白角色为左，第二个为右
    roles_order = []
    for m in messages:
        r = m.get("role", "")
        is_narr = bool(m.get("is_narration")) or r in ("旁白", "narrator", "解说", "画外音")
        if not is_narr and r and r not in roles_order:
            roles_order.append(r)

    def side_of(role: str) -> str:
        """left=头像在左气泡白色（我方/对方1），right=头像在右气泡绿色（我方/对方2）"""
        if role in roles_order:
            return "left" if roles_order.index(role) % 2 == 0 else "right"
        return "left"

    # ---- 查找素材（头像、背景） ----
    avatars = dict(script.get("avatars") or {})
    bg_url = _abs_url(script.get("bg"))

    if asset_dir and os.path.isdir(asset_dir):
        # 自动查找背景
        if not bg_url:
            for bg_name in ("bg.png", "bg.jpg", "bg.jpeg", "background.png",
                            "background.jpg", "bg.webp"):
                bg_path = os.path.join(asset_dir, bg_name)
                if os.path.isfile(bg_path):
                    bg_url = _abs_url(bg_path)
                    break
        # 自动查找头像：A.png=第一个角色(左), B.png=第二个角色(右)
        auto_avatar_map = {"A": 0, "a": 0, "left": 0, "B": 1, "b": 1, "right": 1}
        for fname, idx in auto_avatar_map.items():
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                fpath = os.path.join(asset_dir, fname + ext)
                if os.path.isfile(fpath) and idx < len(roles_order):
                    role_key = roles_order[idx]
                    if role_key not in avatars:
                        avatars[role_key] = fpath
                    break
        # 任意以角色名命名的头像（如"年轻人.png"）
        for role in roles_order:
            if role in avatars:
                continue
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                fpath = os.path.join(asset_dir, role + ext)
                if os.path.isfile(fpath):
                    avatars[role] = fpath
                    break

    # 背景CSS
    bg_css = "#ededed"
    if bg_url:
        bg_css = f'url("{bg_url}") center/cover no-repeat, #ededed'

    max_end = max(_t1(t) for t in timings) if timings else 0
    video_duration = total_duration if total_duration else (max_end + 2.0 / speedup)

    # ---- 构建HTML ----
    parts = []
    parts.append(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: 100%; height: 100%; overflow: hidden; }}
body {{
  font-family: "Microsoft YaHei", "PingFang SC", -apple-system, sans-serif;
  background: {bg_css};
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  position: relative;
}}
/* 顶部半透明渐变遮罩（让背景不干扰文字） */
body::before {{
  content: '';
  position: absolute;
  inset: 0;
  background: rgba(0,0,0,0.1);
  pointer-events: none;
}}
/* 顶部标题（可选） */
.top-title {{
  position: absolute;
  top: 50px;
  left: 0; right: 0;
  text-align: center;
  color: rgba(255,255,255,0.85);
  font-size: 30px;
  font-weight: 600;
  letter-spacing: 3px;
  text-shadow: 0 2px 8px rgba(0,0,0,0.3);
  z-index: 5;
}}
/* 舞台 */
.stage {{
  width: 100%;
  height: 100%;
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0 60px;
  z-index: 2;
}}
/* 单条消息容器 */
.msg-card {{
  position: absolute;
  width: 100%;
  max-width: 860px;
  display: flex;
  flex-direction: column;
  opacity: 0;
  pointer-events: none;
}}
/* 左/右行布局 */
.msg-row {{
  display: flex;
  align-items: flex-start;
  gap: 16px;
  width: 100%;
}}
.msg-row.side-right {{
  flex-direction: row-reverse;
}}
/* 头像：圆角矩形（微信风格） */
.avatar {{
  width: 72px;
  height: 72px;
  border-radius: 8px;
  flex-shrink: 0;
  object-fit: cover;
  background: #c8c8c8;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 32px;
  color: #fff;
  font-weight: 600;
}}
.avatar.img-avatar {{
  font-size: 0;
}}
/* 消息主体区域（角色名+气泡） */
.msg-body {{
  display: flex;
  flex-direction: column;
  max-width: 68%;
  min-width: 80px;
}}
.msg-row.side-right .msg-body {{
  align-items: flex-end;
}}
/* 角色名 */
.msg-name {{
  font-size: 20px;
  color: rgba(0,0,0,0.45);
  margin-bottom: 4px;
  padding: 0 4px;
}}
/* 气泡 */
.bubble {{
  position: relative;
  padding: 18px 24px;
  border-radius: 6px;
  font-size: 34px;
  line-height: 1.4;
  color: #222;
  word-break: break-word;
  font-weight: 400;
  text-align: left;
}}
/* 左侧白色气泡 + 三角 */
.msg-row.side-left .bubble {{
  background: #fff;
  border-top-left-radius: 4px;
}}
.msg-row.side-left .bubble::before {{
  content: '';
  position: absolute;
  left: -10px;
  top: 14px;
  width: 0; height: 0;
  border: 7px solid transparent;
  border-right-color: #fff;
  border-left: 0;
}}
/* 右侧绿色气泡 + 三角 */
.msg-row.side-right .bubble {{
  background: #95ec69;
  border-top-right-radius: 4px;
  color: #222;
}}
.msg-row.side-right .bubble::before {{
  content: '';
  position: absolute;
  right: -10px;
  top: 14px;
  width: 0; height: 0;
  border: 7px solid transparent;
  border-left-color: #95ec69;
  border-right: 0;
}}
/* 气泡内图片（纯表情包消息） */
.bubble.bubble-img {{
  padding: 6px !important;
  background: transparent !important;
  box-shadow: none !important;
}}
.bubble.bubble-img::before {{ display: none !important; }}
.bubble.bubble-img img {{
  max-width: 380px;
  max-height: 380px;
  border-radius: 10px;
  display: block;
}}
/* 下方贴纸 */
.sticker-below {{
  margin-top: 18px;
  display: flex;
  opacity: 0;
}}
.msg-row.side-left .sticker-below {{ justify-content: flex-start; padding-left: 88px; }}
.msg-row.side-right .sticker-below {{ justify-content: flex-end; padding-right: 88px; }}
.sticker-below img {{
  max-width: 240px;
  max-height: 240px;
  object-fit: contain;
}}
/* 旁白样式 */
.msg-card.narration {{
  align-items: center;
}}
.msg-card.narration .msg-row {{
  justify-content: center;
}}
.msg-card.narration .avatar {{ display: none; }}
.msg-card.narration .msg-body {{
  align-items: center;
  max-width: 80%;
}}
.msg-card.narration .msg-name {{ display: none; }}
.msg-card.narration .bubble {{
  background: rgba(0,0,0,0.6) !important;
  color: #fff !important;
  font-style: italic;
  font-size: 32px;
  letter-spacing: 2px;
  padding: 16px 36px;
  border-radius: 30px !important;
  backdrop-filter: blur(6px);
  border: 1px solid rgba(255,255,255,0.1);
  text-align: center;
  box-shadow: 0 4px 20px rgba(0,0,0,0.3);
}}
.msg-card.narration .bubble::before {{ display: none !important; }}
.msg-card.narration .sticker-below {{ display: none; }}

/* ========== 动画 ========== */
@keyframes msgPop {{
  0% {{ opacity: 0; transform: translateY(50px) scale(0.8); }}
  100% {{ opacity: 1; transform: translateY(0) scale(1); }}
}}
@keyframes msgSlideUp {{
  0% {{ opacity: 0; transform: translateY(100px); }}
  100% {{ opacity: 1; transform: translateY(0); }}
}}
@keyframes msgSlideLeft {{
  0% {{ opacity: 0; transform: translateX(-180px); }}
  100% {{ opacity: 1; transform: translateX(0); }}
}}
@keyframes msgSlideRight {{
  0% {{ opacity: 0; transform: translateX(180px); }}
  100% {{ opacity: 1; transform: translateX(0); }}
}}
@keyframes msgZoom {{
  0% {{ opacity: 0; transform: scale(0.3); }}
  100% {{ opacity: 1; transform: scale(1); }}
}}
@keyframes msgBounce {{
  0% {{ opacity: 0; transform: translateY(80px) scale(0.6); }}
  55% {{ opacity: 1; transform: translateY(-12px) scale(1.04); }}
  75% {{ transform: translateY(4px) scale(0.98); }}
  100% {{ opacity: 1; transform: translateY(0) scale(1); }}
}}
@keyframes msgSwing {{
  0% {{ opacity: 0; transform: rotate(-8deg) translateY(40px); }}
  60% {{ opacity: 1; transform: rotate(4deg) translateY(-5px); }}
  100% {{ opacity: 1; transform: rotate(0) translateY(0); }}
}}
@keyframes msgDrop {{
  0% {{ opacity: 0; transform: translateY(-120px); }}
  65% {{ opacity: 1; transform: translateY(8px); }}
  100% {{ opacity: 1; transform: translateY(0); }}
}}
@keyframes msgFlip {{
  0% {{ opacity: 0; transform: perspective(800px) rotateY(60deg); }}
  100% {{ opacity: 1; transform: perspective(800px) rotateY(0); }}
}}
@keyframes msgExit {{
  0% {{ opacity: 1; transform: translateY(0) scale(1); }}
  100% {{ opacity: 0; transform: translateY(-200px) scale(0.85); }}
}}
@keyframes stickerBounce {{
  0% {{ opacity: 0; transform: translateY(40px) scale(0.3); }}
  60% {{ opacity: 1; transform: translateY(-8px) scale(1.12); }}
  100% {{ opacity: 1; transform: translateY(0) scale(1); }}
}}
@keyframes stickerUp {{
  0% {{ opacity: 0; transform: translateY(35px) scale(0.5); }}
  100% {{ opacity: 1; transform: translateY(0) scale(1); }}
}}
@keyframes stickerZoom {{
  0% {{ opacity: 0; transform: scale(0); }}
  70% {{ opacity: 1; transform: scale(1.2); }}
  100% {{ opacity: 1; transform: scale(1); }}
}}
@keyframes stickerWobble {{
  0% {{ opacity: 0; transform: rotate(-18deg) scale(0.5); }}
  50% {{ opacity: 1; transform: rotate(8deg) scale(1.1); }}
  100% {{ opacity: 1; transform: rotate(0) scale(1); }}
}}
@keyframes stickerPop {{
  0% {{ opacity: 0; transform: scale(0.3) translateY(15px); }}
  50% {{ opacity: 1; transform: scale(1.2) translateY(-5px); }}
  100% {{ opacity: 1; transform: scale(1) translateY(0); }}
}}

.anim-msgPop {{ animation: msgPop .45s cubic-bezier(.2,1.4,.4,1) forwards; }}
.anim-msgSlideUp {{ animation: msgSlideUp .45s cubic-bezier(.2,.8,.3,1) forwards; }}
.anim-msgSlideLeft {{ animation: msgSlideLeft .45s cubic-bezier(.2,.8,.3,1) forwards; }}
.anim-msgSlideRight {{ animation: msgSlideRight .45s cubic-bezier(.2,.8,.3,1) forwards; }}
.anim-msgZoom {{ animation: msgZoom .45s cubic-bezier(.2,.8,.3,1) forwards; }}
.anim-msgBounce {{ animation: msgBounce .55s cubic-bezier(.2,1.4,.4,1) forwards; }}
.anim-msgSwing {{ animation: msgSwing .55s cubic-bezier(.2,1.2,.4,1) forwards; }}
.anim-msgDrop {{ animation: msgDrop .55s cubic-bezier(.2,1.2,.4,1) forwards; }}
.anim-msgFlip {{ animation: msgFlip .5s cubic-bezier(.2,.8,.3,1) forwards; }}
.msg-card.exit {{ animation: msgExit .35s ease forwards !important; }}

.anim-stickerBounce {{ animation: stickerBounce .5s cubic-bezier(.2,1.6,.4,1) forwards; }}
.anim-stickerUp {{ animation: stickerUp .4s cubic-bezier(.2,1.4,.4,1) forwards; }}
.anim-stickerZoom {{ animation: stickerZoom .4s cubic-bezier(.2,1.4,.4,1) forwards; }}
.anim-stickerWobble {{ animation: stickerWobble .5s cubic-bezier(.2,1.4,.4,1) forwards; }}
.anim-stickerPop {{ animation: stickerPop .45s cubic-bezier(.2,1.5,.4,1) forwards; }}
</style>
</head>
<body>
""")

    if title:
        parts.append(f'<div class="top-title">{title}</div>')
    parts.append('<div class="stage">')

    last_anim = None
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        is_narration = bool(msg.get("is_narration")) or role in ("旁白", "narrator", "解说", "画外音")
        t = timings[i] if i < len(timings) else None
        start_time = _t0(timings[i])
        # 侧边优先取 timeline 决策（第3位），否则按角色顺序自动分配
        if is_narration:
            side = "center"
        else:
            side = _tslot(t, 3) or side_of(role)
        # 覆盖贴纸优先取 timeline 决策（第5位）
        provided_overlay = _tslot(t, 5)

        # 选择入场动画（优先取 timeline 决策，无则随机）
        if is_narration:
            anim = "msgSlideUp"
        else:
            anim = _tslot(t, 4)
            if not anim:
                anim = random.choice(MSG_ENTRY_ANIMS)
                if anim == last_anim and len(MSG_ENTRY_ANIMS) > 1:
                    anim = random.choice([a for a in MSG_ENTRY_ANIMS if a != last_anim])
        last_anim = anim

        card_class = f"msg-card anim-{anim}"
        if is_narration:
            card_class += " narration"

        parts.append(f'<div class="{card_class}" id="msg{i}" '
                     f'style="animation-delay:{start_time:.3f}s;">')

        # 头像
        avatar_url = _abs_url(avatars.get(role)) if role in avatars else None
        ch = (role or "?").strip()[:1]
        avatar_color = _pick_avatar_color(role)

        if is_narration:
            # 旁白不需要头像
            pass
        else:
            parts.append(f'<div class="msg-row side-{side}">')
            if avatar_url:
                parts.append(f'<img class="avatar img-avatar" src="{avatar_url}">')
            else:
                parts.append(f'<div class="avatar" style="background:{avatar_color};">{ch}</div>')
            parts.append('<div class="msg-body">')
            parts.append(f'<div class="msg-name">{role}</div>')

        # 气泡内容
        img_url = _abs_url(msg.get("image")) if msg.get("image") else None
        sticker_src = provided_overlay or msg.get("sticker")
        sticker_url = _abs_url(sticker_src) if sticker_src else None
        is_img_msg = msg.get("type") == "sticker" or (img_url and not msg.get("text", "").strip())

        if is_narration:
            # 旁白：居中胶囊
            parts.append('<div class="msg-row">')
            parts.append('<div class="msg-body">')
            parts.append(f'<div class="bubble">{msg.get("text", "")}</div>')
            parts.append('</div>')
            parts.append('</div>')
        elif is_img_msg and img_url:
            parts.append(f'<div class="bubble bubble-img"><img src="{img_url}"></div>')
        else:
            parts.append(f'<div class="bubble">{msg.get("text", "")}</div>')

        # 下方贴纸（非旁白、非纯图消息）
        if not is_narration and not is_img_msg and sticker_url:
            s_anim = random.choice(STICKER_ANIMS)
            s_delay = start_time + 0.22 / speedup
            parts.append(f'<div class="sticker-below anim-{s_anim}" '
                         f'style="animation-delay:{s_delay:.3f}s;">')
            parts.append(f'<img src="{sticker_url}">')
            parts.append('</div>')

        if not is_narration:
            parts.append('</div>')  # msg-body
            parts.append('</div>')  # msg-row

        parts.append('</div>')  # msg-card

    parts.append('</div>')

    # JavaScript：新消息入场时旧消息退场
    finish_at = video_duration
    js = """
<script>
(function() {
  var cards = document.querySelectorAll('.msg-card');
  var times = [];
  cards.forEach(function(c) {
    times.push(parseFloat(c.style.animationDelay || '0s'));
  });
  cards.forEach(function(card, idx) {
    setTimeout(function() {
      for (var j = 0; j < idx; j++) {
        if (!cards[j].classList.contains('exit')) {
          cards[j].classList.add('exit');
        }
      }
    }, times[idx] * 1000 + 450);
  });
  setTimeout(function() {
    window.animationFinished = true;
  }, """ + str(int(finish_at * 1000) + 800) + """);
})();
</script>"""
    parts.append(js)
    parts.append('</body></html>')
    return "".join(parts)


def render_chat_scene(script: dict, output_video: str, width=1080, height=1920,
                      msg_timings: list | None = None, speedup: float = 1.0,
                      total_duration: float = None, asset_dir: str = None) -> bool:
    """渲染聊天场景并录屏为视频。"""
    html = build_chat_html(script, msg_timings, speedup=speedup,
                           total_duration=total_duration, asset_dir=asset_dir)
    tmp_dir = tempfile.mkdtemp(prefix="chat_scene_")
    html_path = os.path.join(tmp_dir, "scene.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("❌ 未安装 playwright，请先: pip install playwright && playwright install chromium")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False

    max_end = max(t[1] if isinstance(t, (tuple, list)) else t for t in msg_timings) if msg_timings else 1.2 * len(script.get("messages", []))
    video_dur = total_duration if total_duration else (max_end + 2.0 / speedup)
    max_duration = int(video_dur) + 8

    rec_dir = os.path.join(tmp_dir, "rec")
    os.makedirs(rec_dir, exist_ok=True)
    url = "file:///" + html_path.replace("\\", "/")
    out_dir = os.path.dirname(os.path.abspath(output_video))
    os.makedirs(out_dir, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                record_video_dir=rec_dir,
                record_video_size={"width": width, "height": height},
                viewport={"width": width, "height": height},
            )
            page = context.new_page()
            try:
                page.goto(url)
                page.wait_for_function(
                    "() => window.animationFinished === true",
                    timeout=max_duration * 1000,
                )
                page.wait_for_timeout(800)
            except Exception as e:
                print(f"⚠️ 等待动画信号超时/出错: {e}，保留已录内容")
            context.close()
            browser.close()

        vids = [f for f in os.listdir(rec_dir) if f.endswith(".webm")]
        if not vids:
            print("❌ 未生成视频文件")
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return False
        src = os.path.join(rec_dir, vids[0])
        if os.path.exists(output_video):
            os.remove(output_video)
        shutil.move(src, output_video)
        print(f"✅ 聊天视频已保存: {output_video}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return True
    except Exception as e:
        print(f"❌ 录屏失败: {e}")
        import traceback
        traceback.print_exc()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return False


if __name__ == "__main__":
    demo = {
        "title": "福音吐槽",
        "messages": [
            {"role": "年轻人", "text": "大师，我最近好迷茫啊"},
            {"role": "大师", "text": "你看这杯水"},
            {"role": "年轻人", "text": "我悟了！您是说要像水一样包容万物？"},
            {"role": "大师", "text": "不，这杯水是你刚才碰倒的，赔30"},
        ],
    }
    out = os.path.join(PROJECT_ROOT, "assets", "chat_video", "demo_v4.webm")
    ok = render_chat_scene(demo, out, 1080, 1920,
                           msg_timings=[(0.5, 2.5), (2.5, 4.5), (4.5, 7.5), (7.5, 10.5)],
                           total_duration=11)
    print("渲染结果:", "成功" if ok else "失败")
