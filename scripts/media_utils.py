# -*- coding: utf-8 -*-
"""媒体工具模块 — 图片尺寸检测、GIF帧处理、自适应缩放计算。"""
import os
from typing import Tuple, Optional, Dict, Any


def get_image_size(image_path: str) -> Optional[Tuple[int, int]]:
    """获取图片尺寸 (width, height)，支持 PNG/JPG/JPEG/GIF/WEBP。
    返回 None 表示读取失败。
    """
    if not image_path or not os.path.exists(image_path):
        return None
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            return img.size  # (width, height)
    except ImportError:
        # Pillow 未安装时，使用简单的文件头解析（仅支持 PNG/JPG/GIF）
        return _get_image_size_fallback(image_path)
    except Exception:
        return None


def _get_image_size_fallback(image_path: str) -> Optional[Tuple[int, int]]:
    """无 Pillow 时的简单尺寸解析（支持 PNG/JPG/GIF，不支持 WEBP）。"""
    try:
        ext = os.path.splitext(image_path)[1].lower()
        with open(image_path, "rb") as f:
            if ext == ".gif":
                f.read(6)  # GIF87a/GIF89a
                w = int.from_bytes(f.read(2), "little")
                h = int.from_bytes(f.read(2), "little")
                return (w, h)
            elif ext == ".png":
                f.read(16)
                w = int.from_bytes(f.read(4), "big")
                h = int.from_bytes(f.read(4), "big")
                return (w, h)
            elif ext in (".jpg", ".jpeg"):
                f.read(2)
                b = f.read(1)
                while b and b != b"\xc3":
                    # 查找 SOF0 marker (0xFFC0)
                    while b != b"\xff":
                        b = f.read(1)
                    while b == b"\xff":
                        b = f.read(1)
                    if b[0] in (0xC0, 0xC1, 0xC2):
                        f.read(3)
                        h = int.from_bytes(f.read(2), "big")
                        w = int.from_bytes(f.read(2), "big")
                        return (w, h)
                    else:
                        seg_len = int.from_bytes(f.read(2), "big")
                        f.read(seg_len - 2)
                        b = f.read(1)
    except Exception:
        pass
    return None


def is_animated_gif(image_path: str) -> bool:
    """检测是否为动态 GIF（多帧）。"""
    if not image_path or not os.path.exists(image_path):
        return False
    ext = os.path.splitext(image_path)[1].lower()
    if ext != ".gif":
        return False
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            try:
                img.seek(1)
                return True
            except EOFError:
                return False
    except ImportError:
        return False
    except Exception:
        return False


def get_gif_frame_count(image_path: str) -> int:
    """获取 GIF 帧数。"""
    if not is_animated_gif(image_path):
        return 1 if os.path.exists(image_path) else 0
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            count = 0
            try:
                while True:
                    img.seek(count)
                    count += 1
            except EOFError:
                pass
            return count
    except Exception:
        return 1


def compute_sticker_scale(
    image_path: str,
    canvas_size: Tuple[int, int] = (1080, 1920),
    target_max_ratio: float = 0.38,
    target_min_ratio: float = 0.25,
    base_size: int = 300,
) -> Dict[str, Any]:
    """计算贴纸在剪映画布中的合适缩放和尺寸。

    原则（下半屏居中显示优化）：
      - 贴纸长边占画布短边的 target_max_ratio（默认 38%，约 410px on 1080宽屏）
      - 小图适当放大到 base_size（300px），避免太小看不清
      - 横图/竖图按长边等比缩放，保持宽高比
      - 返回统一的 uniform_scale 供剪映 KeyframeProperty.uniform_scale 使用

    Args:
        image_path: 图片路径
        canvas_size: 画布 (width, height)，默认 1080x1920 (9:16)
        target_max_ratio: 贴纸最大占画布短边比例
        target_min_ratio: 贴纸最小占画布短边比例
        base_size: 最小像素尺寸（小于此的图适当放大）

    Returns:
        dict: {
            "width": int, "height": int,  # 原始尺寸
            "display_w": int, "display_h": int,  # 建议显示尺寸
            "uniform_scale": float,  # 剪映 uniform_scale 值（相对素材原尺寸）
            "is_animated": bool,  # 是否动态 GIF
            "frame_count": int,  # GIF 帧数
        }
    """
    size = get_image_size(image_path)
    if size is None:
        # 读取失败时用默认值（适合下半屏居中的大小）
        cw, ch = canvas_size
        short_side = min(cw, ch)
        dw = int(short_side * 0.35)
        return {
            "width": 300,
            "height": 300,
            "display_w": dw,
            "display_h": dw,
            "uniform_scale": STICKER_UNIFORM_SCALE_DEFAULT,
            "is_animated": False,
            "frame_count": 1,
        }

    w, h = size
    cw, ch = canvas_size
    short_side = min(cw, ch)
    target_max = int(short_side * target_max_ratio)
    target_min = int(short_side * target_min_ratio)

    # 按长边等比缩放到 target_max
    long_side = max(w, h)
    if long_side > target_max:
        scale = target_max / long_side
    elif long_side < base_size:
        scale = base_size / long_side  # 小图适当放大
    else:
        scale = 1.0

    # 确保不小于 target_min
    display_w = int(w * scale)
    display_h = int(h * scale)
    if max(display_w, display_h) < target_min:
        scale = target_min / max(w, h)
        display_w = int(w * scale)
        display_h = int(h * scale)

    # 剪映 uniform_scale 是相对素材原尺寸的比例
    # 注意：剪映中素材默认会填满 track，需要根据素材实际像素和画布比例换算
    # 剪映的 uniform_scale=1.0 表示素材按原始比例显示，我们需要的是相对于画布的比例
    # 实际上剪映的 scale 是相对素材自然尺寸的倍数，这里我们直接用 scale 作为 uniform_scale
    # 但需要保证贴纸不会太大。经验值：在 1080x1920 画布上，uniform_scale=0.3 对应约 324px 宽
    # 我们需要把我们计算的 display_w 转换为剪映的 uniform_scale
    # 剪映中素材默认以"适配画布"方式导入，uniform_scale=1.0 时素材宽度等于画布宽度
    # 所以 uniform_scale = display_w / cw
    uniform_scale = round(display_w / cw, 3)

    return {
        "width": w,
        "height": h,
        "display_w": display_w,
        "display_h": display_h,
        "uniform_scale": uniform_scale,
        "is_animated": is_animated_gif(image_path),
        "frame_count": get_gif_frame_count(image_path),
    }


# 默认缩放（读取图片失败时的兜底，适合下半屏居中）
STICKER_UNIFORM_SCALE_DEFAULT = 0.35


# HTML 渲染中贴纸的尺寸类（根据图片宽高比选择）
def get_html_sticker_size_class(image_path: str) -> str:
    """HTML 渲染时根据图片比例返回 CSS 类名，控制贴纸框大小。"""
    size = get_image_size(image_path)
    if size is None:
        return "sticker-m"
    w, h = size
    ratio = w / h if h > 0 else 1
    if ratio > 1.4:
        return "sticker-wide"   # 横图
    elif ratio < 0.7:
        return "sticker-tall"   # 竖图
    else:
        return "sticker-m"      # 方图


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        p = sys.argv[1]
        print(f"图片: {p}")
        print(f"尺寸: {get_image_size(p)}")
        print(f"动态GIF: {is_animated_gif(p)}")
        print(f"帧数: {get_gif_frame_count(p)}")
        print(f"缩放参数: {compute_sticker_scale(p)}")
    else:
        # 测试 emojis 目录下第一张图
        EMOJI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "emojis")
        if os.path.isdir(EMOJI_DIR):
            for f in os.listdir(EMOJI_DIR):
                if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                    p = os.path.join(EMOJI_DIR, f)
                    print(f"\n测试: {f}")
                    print(f"  尺寸: {get_image_size(p)}")
                    info = compute_sticker_scale(p)
                    print(f"  显示: {info['display_w']}x{info['display_h']}, scale={info['uniform_scale']}")
                    break
