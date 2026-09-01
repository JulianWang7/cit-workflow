"""Figma REST API 客户端 — UI 文本提取 + 界面截图导出。

P70 AI 工程化流水线 req-ingest 阶段消费 S5 Figma UI 引用表：
  - list_frames：列出项目所有顶层 Frame 及 node-id（给 req-ingest 选 frame）
  - get_nodes：取指定节点的子树结构
  - get_texts：递归提取 Frame 下所有 Text 节点的 characters（填充 S5.text）
  - export_image：导出节点为 PNG 到本地（填充 S5.screenshot）

设计依据：E:\\P70项目\\P70_AI工程化流水线设计方案.md §2.3
  - file_key 默认 BQufa5vyGqJhEoMqHagea6（P70 项目）
  - 认证：HTTP Header X-Figma-Token，从环境变量 FIGMA_TOKEN 读取（不写入配置文件）
  - node_id 格式：API 用冒号 `57:13741`，URL/JSON 用连字符 `57-13741`，内部自动转换
  - 纯 stdlib urllib，无新依赖（参照 bugflow.core.zentao 模式）

大文件策略（P70 设计稿 ~400MB）：
  不下载整个文档（GET /v1/files/:key 会拉取全部节点树，超大文件必然超时/断流）。
  改用两个轻量端点：
  - list_frames → GET /v1/files/:key?depth=3（仅根→画板→Frame，不含 Frame 子节点）
  - get_nodes/get_texts → GET /v1/files/:key/nodes?ids=57:13741（仅取指定节点子树）
  export_image → GET /v1/images/:key（独立端点，不依赖文档树）

约束（记忆 no-extra-config-layers）：Token 走环境变量，不新增 config_status 分支、
不写 zentao.yaml 同类的 figma.yaml。若 FIGMA_TOKEN 未设，工具返回明确错误引导用户设置。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger("bugflow.core.figma")

# P70 项目默认 file_key（设计 §2.3.2）
_DEFAULT_FILE_KEY = "BQufa5vyGqJhEoMqHagea6"

# Figma REST API 基址
_API_BASE = "https://api.figma.com/v1"

# 缓存 TTL（10 分钟）
_CACHE_TTL = 600.0

# 浅层文档缓存：file_key → (document_dict, cached_at)
# depth=3 的文档树（根→画板→Frame），用于 list_frames
_shallow_cache: dict[str, tuple[dict, float]] = {}

# 节点缓存：file_key → {node_id_api → (node_document, cached_at)}
# GET /v1/files/:key/nodes?ids=... 的结果，用于 get_nodes/get_texts
_node_cache: dict[str, dict[str, tuple[dict, float]]] = {}


# ── 异常 ──────────────────────────────────────────────────────
class FigmaError(Exception):
    """Figma API 调用失败（瞬态，可重试）。"""


class FigmaAuthError(FigmaError):
    """鉴权失败（FIGMA_TOKEN 未设或无效，不可重试）。"""


# ── 配置 ──────────────────────────────────────────────────────
def _read_token() -> str:
    """从环境变量 FIGMA_TOKEN 读取，缺失报 FigmaAuthError。"""
    token = os.environ.get("FIGMA_TOKEN", "").strip()
    if not token:
        raise FigmaAuthError(
            "未配置 Figma Token。请在环境变量中设置 FIGMA_TOKEN "
            "（Figma → Settings → Account → Personal access tokens 生成）。"
            "不写入配置文件，仅走环境变量（设计 §2.3.2）。"
        )
    return token


def _node_id_to_api(node_id: str) -> str:
    """URL/连字符格式 → API 冒号格式。57-13741 → 57:13741。"""
    return node_id.replace("-", ":")


def _node_id_from_api(node_id: str) -> str:
    """API 冒号格式 → URL/连字符格式（与 P70_features.json 一致）。57:13741 → 57-13741。"""
    return node_id.replace(":", "-")


# ── HTTP ──────────────────────────────────────────────────────
def _figma_get(path: str, timeout: float = 30) -> dict:
    """GET Figma API，返回解析后的 JSON dict。

    HTTP 401/403 → FigmaAuthError（不可重试）
    网络/超时/4xx/5xx → FigmaError（transient）
    """
    token = _read_token()
    url = f"{_API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Figma-Token", token)
    req.add_header("Accept", "application/json")

    logger.debug("Figma GET %s", path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise FigmaAuthError(
                f"Figma 鉴权失败 (HTTP {e.code})，请检查环境变量 FIGMA_TOKEN 是否有效"
            ) from e
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise FigmaError(f"Figma API HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, OSError) as e:
        raise FigmaError(f"Figma API 连接失败: {e}") from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise FigmaError(f"Figma API 响应解析失败: {e}; body={raw[:200]}") from e


def _figma_get_bytes(path: str, timeout: float = 60) -> bytes:
    """GET Figma API，返回原始 bytes（用于图片导出）。"""
    token = _read_token()
    url = f"{_API_BASE}/{path.lstrip('/')}"
    req = urllib.request.Request(url, method="GET")
    req.add_header("X-Figma-Token", token)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise FigmaAuthError(
                f"Figma 鉴权失败 (HTTP {e.code})，请检查 FIGMA_TOKEN"
            ) from e
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise FigmaError(f"Figma API HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, OSError) as e:
        raise FigmaError(f"Figma API 连接失败: {e}") from e


# ── 文档加载（轻量端点，不下载整个文件）─────────────────────
def _load_document_shallow(file_key: str, depth: int = 3, use_cache: bool = True) -> dict:
    """GET /v1/files/:key?depth=N，返回 document 节点（浅层，带缓存）。

    depth=3 取根→画板(CANVAS)→Frame，不含 Frame 子节点。
    用于 list_frames 列出顶层 Frame 名称和 ID。
    比下载整个文档（P70 ~400MB）小几个数量级。
    """
    import time

    now = time.time()
    if use_cache:
        cached = _shallow_cache.get(file_key)
        if cached and now - cached[1] < _CACHE_TTL:
            return cached[0]

    data = _figma_get(f"/files/{file_key}?depth={depth}")
    document = data.get("document", {}) if isinstance(data, dict) else {}
    if not document:
        raise FigmaError(f"Figma 文件 {file_key} 无 document 字段（响应: {str(data)[:200]}）")
    _shallow_cache[file_key] = (document, now)
    return document


def _load_node(file_key: str, node_id_api: str, use_cache: bool = True) -> dict:
    """GET /v1/files/:key/nodes?ids=57:13741，返回指定节点的 document 子树。

    只下载请求的节点子树，不拉取整个文件。
    用于 get_nodes/get_texts 获取特定 Frame 的结构或文本。
    """
    import time

    now = time.time()
    file_nodes = _node_cache.setdefault(file_key, {})
    if use_cache:
        cached = file_nodes.get(node_id_api)
        if cached and now - cached[1] < _CACHE_TTL:
            return cached[0]

    encoded_id = urllib.parse.quote(node_id_api)
    data = _figma_get(f"/files/{file_key}/nodes?ids={encoded_id}", timeout=60)
    nodes_map = data.get("nodes", {}) if isinstance(data, dict) else {}
    # nodes_map 的 key 可能是请求的 id 或其变体
    entry = nodes_map.get(node_id_api) or next(iter(nodes_map.values()), None)
    node_doc = (entry or {}).get("document", {}) if isinstance(entry, dict) else {}
    if not node_doc:
        err = (entry or {}).get("err", "") if isinstance(entry, dict) else ""
        raise FigmaError(
            f"Figma 节点 {node_id_api} 不存在或无法加载"
            + (f"（err={err}）" if err else "")
            + f"（file_key={file_key}）"
        )
    file_nodes[node_id_api] = (node_doc, now)
    return node_doc


# ── 递归遍历辅助 ─────────────────────────────────────────────
def _iter_canvases(document: dict) -> list[dict]:
    """document → CANVAS 子节点列表（Figma 文件每页一个 CANVAS）。"""
    return [c for c in document.get("children", []) if c.get("type") == "CANVAS"]


def _walk_texts(node: dict) -> list[dict]:
    """递归收集所有 TEXT 类型节点。"""
    texts: list[dict] = []
    if node.get("type") == "TEXT":
        texts.append(node)
    for child in node.get("children", []) or []:
        texts.extend(_walk_texts(child))
    return texts


# ═══════════════════════════════════════════════════════════════
# 4 个对外函数（对应 4 个 MCP 工具）
# ═══════════════════════════════════════════════════════════════

def list_frames(file_key: str = "", use_cache: bool = True) -> str:
    """列出 Figma 文件所有顶层 Frame 及其 node-id。

    遍历每个 CANVAS（页面）下的 FRAME 节点，输出 frame 名称 + node-id（连字符格式，
    与 P70_features.json 一致）。req-ingest 据此选择 S5 的 Figma Frame。

    使用 depth=3 浅层加载（根→画板→Frame），不下载 Frame 子节点，
    避免对超大设计文件（如 P70 ~400MB）下载整个文档。

    Args:
        file_key: Figma 文件 key，默认 P70 项目 BQufa5vyGqJhEoMqHagea6
        use_cache: 是否使用文件树缓存（默认 True）
    """
    fk = file_key.strip() or _DEFAULT_FILE_KEY
    document = _load_document_shallow(fk, depth=3, use_cache=use_cache)
    canvases = _iter_canvases(document)
    if not canvases:
        return f"❌ Figma 文件 {fk} 未找到任何页面（CANVAS）。"

    lines = [f"=== Figma 文件 {fk} 顶层 Frame 列表 ==="]
    total = 0
    for canvas in canvases:
        page_name = canvas.get("name", "(未命名页面)")
        # depth=3 时 CANVAS 的直接子节点已包含，取其中的 FRAME
        top_frames = [f for f in canvas.get("children", []) or [] if f.get("type") == "FRAME"]
        if not top_frames:
            continue
        lines.append(f"\n[页面] {page_name}")
        for f in top_frames:
            nid = _node_id_from_api(f.get("id", ""))
            name = f.get("name", "(未命名)")
            lines.append(f"  {nid}  {name}")
            total += 1
    lines.append(f"\n共 {total} 个顶层 Frame。")
    return "\n".join(lines)


def get_nodes(file_key: str = "", node_id: str = "", use_cache: bool = True) -> str:
    """获取指定 Figma 节点的子节点树结构。

    返回节点类型树（含 id/name/type），不递归展开 TEXT 节点的 characters。
    用于 req-ingest 理解界面结构、选择要提取文本的子节点。

    使用 /v1/files/:key/nodes?ids=... 端点，只下载指定节点的子树，
    不下载整个文档（避免对超大文件超时）。

    Args:
        file_key: Figma 文件 key，默认 P70 项目
        node_id: 节点 ID（连字符或冒号格式均可，内部自动转换）
        use_cache: 是否使用节点缓存（默认 True）
    """
    fk = file_key.strip() or _DEFAULT_FILE_KEY
    if not node_id.strip():
        return "❌ 请指定 node_id（从 list_frames 获取）。"

    nid_api = _node_id_to_api(node_id.strip())
    try:
        node = _load_node(fk, nid_api, use_cache=use_cache)
    except FigmaError as e:
        return f"❌ 获取节点 {node_id} 失败: {e}"

    lines = [f"=== 节点 {_node_id_from_api(node.get('id', nid_api))} 子树 ==="]
    _format_node_tree(node, lines, depth=0, max_depth=4)
    return "\n".join(lines)


def _format_node_tree(node: dict, lines: list[str], depth: int, max_depth: int) -> None:
    """递归格式化节点树（限制深度避免输出过长）。"""
    if depth > max_depth:
        return
    indent = "  " * depth
    nid = _node_id_from_api(node.get("id", ""))
    ntype = node.get("type", "?")
    name = node.get("name", "")
    label = f"{indent}- [{ntype}] {name}" + (f"  ({nid})" if nid else "")
    lines.append(label)
    for child in node.get("children", []) or []:
        _format_node_tree(child, lines, depth + 1, max_depth)


def get_texts(file_key: str = "", node_id: str = "", use_cache: bool = True) -> str:
    """提取指定 Figma 节点下所有 Text 节点的文本（characters 字段）。

    req-ingest 读到 S5 有 Figma frame 但 text 列为空时，调此工具自动提取 UI 文本填充 S5。
    输出每行一个文本（去重 + 去空白），供 req-design 的 ui_wait_text 用例使用。

    使用 /v1/files/:key/nodes?ids=... 端点，只下载指定节点的子树。
    node_id 为空时尝试浅层加载整个文件（depth=3，可能不含深层 TEXT 节点）。

    Args:
        file_key: Figma 文件 key，默认 P70 项目
        node_id: 节点 ID（连字符或冒号格式均可）；空则尝试整个文件
        use_cache: 是否使用缓存（默认 True）
    """
    fk = file_key.strip() or _DEFAULT_FILE_KEY

    if node_id.strip():
        nid_api = _node_id_to_api(node_id.strip())
        try:
            node = _load_node(fk, nid_api, use_cache=use_cache)
        except FigmaError as e:
            return f"❌ 获取节点 {node_id} 失败: {e}"
        texts = _walk_texts(node)
        scope = f"节点 {_node_id_from_api(nid_api)}"
    else:
        # 无 node_id：浅层加载（depth=3），可能不含深层 TEXT
        document = _load_document_shallow(fk, depth=3, use_cache=use_cache)
        texts = _walk_texts(document)
        scope = "整个文件（浅层 depth=3）"

    if not texts:
        return f"❌ {scope} 未找到任何 Text 节点。"

    # 去重 + 去空白，保留顺序
    seen: set[str] = set()
    unique: list[str] = []
    for t in texts:
        chars = (t.get("characters") or "").strip()
        if chars and chars not in seen:
            seen.add(chars)
            unique.append(chars)

    lines = [f"=== {scope} 的 UI 文本（{len(unique)} 条，已去重）==="]
    for s in unique:
        lines.append(f"  {s}")
    return "\n".join(lines)


def export_image(
    file_key: str = "",
    node_id: str = "",
    save_path: str = "",
    scale: float = 1.0,
) -> str:
    """导出指定 Figma 节点为 PNG 截图到本地。

    req-ingest 读到 S5 需要截图时，调此工具导出 PNG 到 文档/figma/ 目录。
    Figma images 端点返回 {images: {node_id: url}}，再下载 PNG bytes 写本地。
    此端点独立于文档树加载，不受大文件影响。

    Args:
        file_key: Figma 文件 key，默认 P70 项目
        node_id: 节点 ID（连字符或冒号格式均可）
        save_path: 本地保存路径（含文件名）；空则存 ~/.bugfix-flow/figma/{node_id}.png
        scale: 导出缩放，默认 1.0（2x 用于高分屏截图）
    """
    fk = file_key.strip() or _DEFAULT_FILE_KEY
    if not node_id.strip():
        return "❌ 请指定 node_id（从 list_frames 获取）。"

    nid_api = _node_id_to_api(node_id.strip())
    nid_hyphen = _node_id_from_api(nid_api)

    # 1. 获取图片 URL
    params = urllib.parse.urlencode({
        "ids": nid_api,
        "format": "png",
        "scale": float(scale),
    })
    try:
        data = _figma_get(f"/images/{fk}?{params}", timeout=60)
    except FigmaError as e:
        return f"❌ 获取图片 URL 失败: {e}"

    images_map = data.get("images", {}) if isinstance(data, dict) else {}
    img_url = images_map.get(nid_api) or images_map.get(nid_hyphen)
    if not img_url:
        # 兜底：取第一个值
        img_url = next(iter(images_map.values()), None) if images_map else None
    if not img_url:
        err = data.get("err", "") if isinstance(data, dict) else ""
        return f"❌ Figma 未返回图片 URL（err={err}）。可能节点 {node_id} 不支持导出。"

    # 2. 下载 PNG bytes
    try:
        png_bytes = _download_image_bytes(img_url)
    except FigmaError as e:
        return f"❌ 下载 PNG 失败: {e}"

    # 3. 写本地文件
    if not save_path.strip():
        from bugflow.core.config import _config_dir
        save_dir = str(_config_dir() / "figma")
        os.makedirs(save_dir, exist_ok=True)
        save_path = os.path.join(save_dir, f"{nid_hyphen}.png")
    else:
        save_path = os.path.abspath(save_path)
        os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)

    with open(save_path, "wb") as f:
        f.write(png_bytes)

    size_kb = len(png_bytes) / 1024
    return f"✅ 已导出: {save_path} ({size_kb:.1f} KB, node_id={nid_hyphen})"


def _download_image_bytes(url: str, timeout: float = 60) -> bytes:
    """下载 Figma images 端点返回的临时 URL（S3 签名链接，无 token）。"""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise FigmaError(f"下载图片 HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:200]}") from e
    except (urllib.error.URLError, OSError) as e:
        raise FigmaError(f"下载图片连接失败: {e}") from e
