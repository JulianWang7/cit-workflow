# -*- coding: utf-8 -*-
"""视觉识图 — 两级策略让纯文本主推理模型（glm-5.2）"间接看图"。

策略（参考 YWAgent ocr_tool.py 的 local-first 设计）：
  1. **Windows 本地 OCR（winrt）** — 仅 Windows 10/11，调用系统内置 OCR 引擎。
     专为截图优化，~2s 出结果，不耗 token，不触网络（无 30s MCP 超时压力）。
     大多数 bugfix 截图是 logcat/代码/日志文字，OCR 直接搞定。
  2. **LLM Vision API** — OCR 无文字时 fallback 到视觉模型（默认 qwen3.7-plus），
     描述 UI 元素、异常现象、布局等视觉信息（OCR 做不到的）。

glm-5.2 的 supports_image=False，图片在 MCP normalizer 里会被剥离，
所以这里由插件**自己**调 winrt OCR / 视觉 API，只把**文本**回传给主模型——
主模型全程主导推理，视觉/OCR 只当"眼"。

可选依赖（缺失时自动降级，不阻断流程）：
- Pillow — 大图压缩（超 512KB 时转 JPEG 降质量 + 缩尺寸）
- winrt  — Windows 10/11 原生 OCR 引擎

复用 core 层：
- core.model_detect.get_api_credentials()  → base_url + 解密 api_key
- core.model_detect._post_chat()           → POST /chat/completions
- core.config.load_llm_config()            → vision_model 可选覆盖

视觉模型默认 qwen3.7-plus。配置覆盖：llm.yaml 的 llm: 节加 vision_model。

设计红线：任何环节失败都静默降级返回错误文本，绝不抛异常阻断 bugfix 流程。
"""

from __future__ import annotations

import base64
import logging
import os
from io import BytesIO
from pathlib import Path

from .config import load_llm_config
from .model_detect import get_api_credentials, _post_chat

logger = logging.getLogger("core.vision")

# ── 配置 ──────────────────────────────────────────────────────────

DEFAULT_VISION_MODEL = "qwen3.7-plus"

# base64 膨胀约 4/3，原图 512KB → base64 ~683KB + JSON 框架，留余量取 512KB。
# 超过此阈值时启用 PIL 压缩（转 JPEG 降质量），从源头减小传输 + 推理时间。
MAX_IMAGE_BYTES = 512 * 1024

# Windows OCR 最大耗时（秒）。超时则放弃，fallback 到视觉 API。
# OCR 通常 ~0.1-2s；8s 是安全上限。
_OCR_TIMEOUT = 8

# 视觉 API HTTP 超时（秒）。MCP 框架 30s 硬杀，OCR ~0.1s，留 26s 给 API。
# 内网 LLM 处理 50KB 图片 ~20-25s，26s 是临界值；超时后降级到 OCR 文本。
_API_TIMEOUT = 26

# 视觉约束 prompt：强制"只描述、不推理"——分工关键。
# 视觉模型只输出所见事实，根因/修复决策留给主推理模型。
_VISION_CONSTRAINT = (
    "你是视觉感知器。只描述你在图中**所见的事实**：UI 元素、可见文字、"
    "布局、颜色、异常区域（白屏/花屏/错位/遮挡）、明显的视觉现象。"
    "不要推测根因、不要给修复建议、不要编造图外的信息。"
    "用简洁的中文分点描述。"
)


def _vision_model() -> str:
    """读 llm.yaml 的 vision_model 覆盖，默认 qwen3.7-plus。"""
    try:
        cfg = load_llm_config()
        return cfg.get("vision_model") or DEFAULT_VISION_MODEL
    except Exception:
        return DEFAULT_VISION_MODEL


# ── 图像字节提取 ──────────────────────────────────────────────────

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
}


def _image_to_bytes(image: str) -> tuple[bytes, str]:
    """从路径 / base64 / data URL 提取原始字节 + MIME 类型。

    Returns:
        (raw_bytes, mime_type)
    Raises:
        FileNotFoundError: 文件不存在
        ValueError: 无法识别格式或文件为空
    """
    # 已是 data URL → 解码 base64 部分
    if image.startswith("data:"):
        header, b64 = image.split(",", 1) if "," in image else (image, "")
        mime = "image/png"
        if ":" in header:
            mime = header.split(":")[1].split(";")[0] or "image/png"
        return base64.b64decode(b64), mime

    # 纯 base64（无路径分隔符且能解码）→ 补前缀
    try:
        if not os.path.isabs(image) and not os.path.exists(image) and "/" not in image and "\\" not in image:
            base64.b64decode(image, validate=True)
            return base64.b64decode(image), "image/png"
    except Exception:
        pass  # 不是 base64，按文件路径走

    # 文件路径
    p = Path(image)
    if not p.is_file():
        raise FileNotFoundError(f"图像文件不存在: {image}")
    raw = p.read_bytes()
    if not raw:
        raise ValueError(f"图像文件为空: {image}")
    mime = _MIME_BY_EXT.get(p.suffix.lower(), "image/png")
    return raw, mime


def _bytes_to_data_url(image_data: bytes, mime: str = "image/png") -> tuple[str, int]:
    """原始字节 → data URL。Returns: (data_url, raw_len)。"""
    b64 = base64.b64encode(image_data).decode("ascii")
    return f"data:{mime};base64,{b64}", len(image_data)


def _image_to_data_url(image: str) -> tuple[str, int]:
    """路径 / base64 / data URL → data URL（兼容 ping_vision）。

    内部委托 _image_to_bytes + _bytes_to_data_url。
    """
    raw, mime = _image_to_bytes(image)
    return _bytes_to_data_url(raw, mime)


# ── 图像压缩 ──────────────────────────────────────────────────────

def _compress_image(image_data: bytes, max_size: int = MAX_IMAGE_BYTES) -> tuple[bytes, str]:
    """用 PIL 压缩图像到 max_size 以内。

    超过阈值时转 JPEG + 逐步降质量 + 缩尺寸。
    小于阈值直接返回原图（保持原 MIME，不转格式）。

    Returns:
        (compressed_bytes, mime) — mime="image/jpeg" 表示已压缩，
        mime="" 表示未压缩（原始 MIME 由调用方决定）。
    """
    if len(image_data) <= max_size:
        return image_data, ""

    try:
        from PIL import Image
    except ImportError:
        logger.debug("Pillow 未安装，跳过压缩")
        return image_data, ""

    try:
        img = Image.open(BytesIO(image_data))
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")

        quality = 85
        while quality > 10:
            buf = BytesIO()
            # 图像太大时先缩尺寸再降质量
            if len(image_data) > max_size * 2:
                img = img.resize((max(img.width // 2, 1), max(img.height // 2, 1)))
            img.save(buf, format="JPEG", quality=quality)
            compressed = buf.getvalue()
            if len(compressed) <= max_size:
                logger.info(
                    "图像压缩: %d → %d bytes (JPEG q=%d)",
                    len(image_data), len(compressed), quality,
                )
                return compressed, "image/jpeg"
            quality -= 15
        # 最低质量仍然太大，返回最后结果
        logger.warning("图像压缩后仍超阈值: %d bytes", len(compressed))
        return compressed, "image/jpeg"
    except Exception as e:
        logger.debug("PIL 压缩失败: %s", e)
        return image_data, ""


# ── Windows 本地 OCR ─────────────────────────────────────────────

async def _ocr_windows_async(image_data: bytes) -> str:
    """winrt 异步 OCR，返回提取的文字（可能为空）。

    在独立线程的新事件循环中执行（MCP server 可能已有事件循环）。
    """
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    from winrt.windows.graphics.imaging import (
        BitmapDecoder, BitmapPixelFormat, SoftwareBitmap,
    )
    from winrt.windows.storage.streams import (
        InMemoryRandomAccessStream, DataWriter,
    )

    engine = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
    if engine is None:
        engine = OcrEngine.try_create()
    if engine is None:
        raise RuntimeError("Windows OCR 引擎创建失败（系统可能不支持）")

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    # PyWinRT 新版接受 bytes，旧版接受 list[int]
    try:
        writer.write_bytes(image_data)
    except TypeError:
        writer.write_bytes(list(image_data))
    await writer.store_async()
    stream.seek(0)

    decoder = await BitmapDecoder.create_async(stream)
    software_bitmap = await decoder.get_software_bitmap_async()

    bgra8 = getattr(BitmapPixelFormat, "BGRA8", None) or getattr(BitmapPixelFormat, "bgra8", None)
    if bgra8 is not None and software_bitmap.bitmap_pixel_format != bgra8:
        software_bitmap = SoftwareBitmap.convert(software_bitmap, bgra8)

    result = await engine.recognize_async(software_bitmap)
    return (result.text or "").strip()


def _run_ocr_in_thread(image_data: bytes) -> str:
    """在新线程中创建独立事件循环跑 winrt OCR。"""
    import asyncio
    return asyncio.run(_ocr_windows_async(image_data))


def _ocr_windows(image_data: bytes) -> tuple[bool, str]:
    """Windows 本地 OCR（winrt），返回 (ok, text_or_error)。

    仅 Windows 10/11 + PyWinRT 可用。不可用时返回 (False, ...)，调用方 fallback。
    OCR 成功但图片无文字 → (True, "")。
    """
    try:
        import winrt.windows.media.ocr  # noqa: F401
    except ImportError:
        return False, "Windows OCR 不可用（未安装 winrt）"

    try:
        from concurrent.futures import ThreadPoolExecutor

        # MCP server 可能已有事件循环，在新线程中跑 asyncio.run
        with ThreadPoolExecutor(max_workers=1) as pool:
            text = pool.submit(_run_ocr_in_thread, image_data).result(timeout=_OCR_TIMEOUT)
        if text:
            return True, text
        return True, ""  # OCR 成功但图片无文字
    except Exception as e:
        logger.debug("Windows OCR 失败: %s", e)
        return False, f"Windows OCR 错误: {e}"


# ── 识图 ──────────────────────────────────────────────────────────

# 触发 OCR 快速通道的关键词（问题中包含这些词 → 只需文字，不需视觉描述）
_TEXT_QUERY_KEYWORDS = (
    "文字", "text", "ocr", "读", "提取", "read", "extract",
    "什么字", "写了", "内容是", "说的是",
)


def _is_text_query(question: str) -> bool:
    """判断问题是否仅关于文字提取（可跳过视觉 API）。"""
    if not question:
        return False
    q = question.lower()
    return any(kw in q for kw in _TEXT_QUERY_KEYWORDS)


def describe_image(
    image: str,
    question: str = "",
    model: str = "",
    timeout: int = _API_TIMEOUT,
) -> str:
    """两级识图：Windows OCR 快速通道 → LLM Vision API（含 OCR 辅助）。

    策略：
      - **文字提取类问题**（"图中写了什么"/"提取文字"）→ OCR 命中即返回，跳过 API（~0.1s）。
      - **视觉描述类问题**（默认/"描述异常现象"）→ 调视觉 API，同时把 OCR 文字
        作为辅助上下文喂给模型（模型既有图又有文字，描述更准）。
      - API 失败时若有 OCR 文本 → 降级返回 OCR 文本。

    Args:
        image: 本地图像路径 / base64 字符串 / data URL。
        question: 要视觉模型回答的问题。空则用默认视觉描述任务。
        model: 视觉模型名，空则用 _vision_model()（默认 qwen3.7-plus）。
        timeout: 视觉 API HTTP 超时秒（默认 20，适配 MCP 30s 窗口）。

    Returns:
        OCR 文本（文字查询命中时）或视觉模型文本描述。
        失败时返回 "⚠️ 视觉识别失败: <原因>"，不抛异常（调用方可据此降级）。
    """
    # 1. 提取原始字节
    try:
        raw_bytes, mime = _image_to_bytes(image)
    except Exception as e:
        msg = f"⚠️ 视觉识别失败：图像读取失败 ({e})"
        logger.warning(msg)
        return msg

    # 2. Windows 本地 OCR（~0.1-2s，无网络，不耗 token）
    #    始终执行：文字查询时可直接返回；视觉查询时作为 API 辅助上下文。
    ocr_ok, ocr_text = _ocr_windows(raw_bytes)
    if not ocr_ok:
        logger.debug("Windows OCR 不可用: %s", ocr_text)

    # 3. 文字提取类问题 + OCR 命中 → 快速通道，跳过 API
    if ocr_ok and ocr_text and _is_text_query(question):
        logger.info("文字查询 + OCR 命中 (%d chars)，跳过视觉 API", len(ocr_text))
        return ocr_text

    # 4. 视觉 API 路径 — 凭证
    try:
        base_url, api_key = get_api_credentials()
    except Exception as e:
        # API 不可用但 OCR 有文本 → 降级返回
        if ocr_ok and ocr_text:
            logger.warning("API 凭证获取失败，降级返回 OCR 文本: %s", e)
            return ocr_text
        msg = f"⚠️ 视觉识别失败：拿不到 API 凭证 ({e})"
        logger.warning(msg)
        return msg
    if not base_url or not api_key:
        if ocr_ok and ocr_text:
            logger.warning("API 凭证为空，降级返回 OCR 文本")
            return ocr_text
        msg = "⚠️ 视觉识别失败：base_url 或 api_key 为空，请先配置 provider JSON。"
        logger.warning(msg)
        return msg

    # 5. 压缩（超阈值时用 PIL 真压缩，而非只设 detail:low 标志）
    use_bytes, use_mime = raw_bytes, mime
    if len(raw_bytes) > MAX_IMAGE_BYTES:
        compressed, comp_mime = _compress_image(raw_bytes)
        if comp_mime:
            use_bytes, use_mime = compressed, comp_mime
        else:
            logger.warning(
                "图像 %d bytes 超过阈值 %d，压缩失败使用原图",
                len(raw_bytes), MAX_IMAGE_BYTES,
            )

    # 6. 编码为 data URL
    data_url, raw_len = _bytes_to_data_url(use_bytes, use_mime)

    # 7. 构造多模态 payload（OpenAI 兼容）
    model_id = model or _vision_model()
    user_text = _VISION_CONSTRAINT
    if question:
        user_text = f"{_VISION_CONSTRAINT}\n\n具体问题：{question}"

    # OCR 文字作为辅助上下文（帮助模型识别小字/代码/日志等 OCR 擅长的内容）
    if ocr_ok and ocr_text:
        # 截断防 prompt 过长
        ocr_snippet = ocr_text[:800]
        user_text += f"\n\n图中 OCR 识别文字（供参考，可能有误）：{ocr_snippet}"

    image_block: dict = {"type": "image_url", "image_url": {"url": data_url}}
    # 压缩后仍超阈值 → 加 detail:low（低分辨率模式，省 token + 缩 body）
    if raw_len > MAX_IMAGE_BYTES:
        image_block["image_url"]["detail"] = "low"

    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    image_block,
                ],
            }
        ],
        "max_tokens": 1024,  # 描述任务不需要长输出
    }

    # 8. 调 API
    r = _post_chat(api_key, base_url, payload, timeout=timeout)
    if not r["ok"]:
        # API 失败但 OCR 有文本 → 降级返回
        if ocr_ok and ocr_text:
            logger.warning("API 返回 HTTP %s，降级返回 OCR 文本", r.get("status"))
            return ocr_text
        body_preview = str(r.get("body", ""))[:200]
        msg = f"⚠️ 视觉识别失败：API 返回 HTTP {r.get('status')} — {body_preview}"
        logger.warning(msg)
        return msg

    # 9. 解析
    try:
        data = r["data"]
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            # 部分多模态模型回 content 数组，取 text 块拼接
            content = "".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        usage = data.get("usage", {})
        if usage.get("prompt_tokens_details", {}).get("image_tokens"):
            logger.info(
                "视觉识别成功: %d image_tokens, %d output tokens",
                usage["prompt_tokens_details"]["image_tokens"],
                usage.get("completion_tokens", 0),
            )
        return content.strip()
    except (KeyError, IndexError, TypeError) as e:
        # 解析失败但 OCR 有文本 → 降级返回
        if ocr_ok and ocr_text:
            logger.warning("API 响应解析异常，降级返回 OCR 文本: %s", e)
            return ocr_text
        msg = f"⚠️ 视觉识别失败：响应解析异常 ({e})"
        logger.warning(msg)
        return msg


def ping_vision(model: str = "") -> tuple[bool, str]:
    """探测视觉通道是否可用（给 config 状态检查用）。

    Returns:
        (ok, message)
    """
    model_id = model or _vision_model()
    try:
        base_url, api_key = get_api_credentials()
    except Exception as e:
        return False, f"凭证获取失败: {e}"
    if not base_url or not api_key:
        return False, "base_url/api_key 未配置"

    # 用 1x1 透明 PNG 探测，省 token
    import struct
    import zlib
    raw = b"\x00\x00\x00\x00\x00"  # filter + 1px RGBA
    compressed = zlib.compress(raw)
    tag = b"IHDR"
    ihdr = tag + struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += struct.pack(">I", 13) + ihdr + struct.pack(">I", zlib.crc32(ihdr) & 0xFFFFFFFF)
    png += struct.pack(">I", len(compressed)) + b"IDAT" + compressed + struct.pack(">I", zlib.crc32(b"IDAT" + compressed) & 0xFFFFFFFF)
    png += struct.pack(">I", 0) + b"IEND" + struct.pack(">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    b64 = base64.b64encode(png).decode("ascii")

    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "这是测试图，回复 OK"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "max_tokens": 16,
    }
    r = _post_chat(api_key, base_url, payload, timeout=30)
    if r["ok"]:
        return True, f"视觉通道可用 (model={model_id})"
    return False, f"HTTP {r.get('status')}: {str(r.get('body', ''))[:120]}"
