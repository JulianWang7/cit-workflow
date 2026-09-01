# -*- coding: utf-8 -*-
"""从 85 SMB 共享浏览/拉取固件包，解压到本地供整刷。

依赖 Windows 凭据管理器缓存的 SMB 凭证，无需额外配置账号密码。
首次使用请在资源管理器访问 \\\\192.168.0.85 并勾选"记住凭据"。

固件版本库结构（\\192.168.0.85\\软件版本\\）：
  {产品}\\
    └─ {子目录}\\          不预设名称，动态列出（正式版本/Trigger/...）
        └─ {版本}\\        按日期或日期_时间命名
            └─ *.zip       主固件包 + _ota/_symbol/_target_files 辅助包

典型流程：list_products → browse_product → list_versions → fetch_firmware
"""

from __future__ import annotations

import os
import shutil
import zipfile
from pathlib import Path

from .config import _config_dir

# 85 共享上的固件版本库
_FW_SHARE = Path(r"\\192.168.0.85\软件版本")

# 偏好文件：记住上次固件缓存目录
_CACHE_DIR_PREF = _config_dir() / ".firmware_cache_dir"

# 排除的 zip 后缀（非主固件包）
_EXCLUDE_SUFFIXES = ("_ota.zip", "_symbol.zip", "_target_files.zip")


def _fmt_size(n: int) -> str:
    """字节数 → 人类可读。"""
    if n >= 1024 * 1024 * 1024:
        return f"{n / 1024 / 1024 / 1024:.1f}GB"
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.0f}MB"
    if n >= 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n}B"


def _share_reachable() -> bool:
    """探测 85 SMB 固件共享是否可达。"""
    try:
        return _FW_SHARE.exists()
    except OSError:
        return False


def _unreachable_msg() -> str:
    return (
        "❌ 85 服务器固件共享不可达：\\\\192.168.0.85\\软件版本\n"
        "请在资源管理器访问 \\\\192.168.0.85 并勾选\"记住凭据\"。"
    )


def _load_cache_dir_pref() -> str:
    """读取上次保存的固件缓存目录偏好。"""
    try:
        if _CACHE_DIR_PREF.exists():
            return _CACHE_DIR_PREF.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _save_cache_dir_pref(local_dir: str) -> None:
    """保存固件缓存目录偏好，下次自动复用。"""
    try:
        _CACHE_DIR_PREF.write_text(local_dir, encoding="utf-8")
    except OSError:
        pass


def list_products(keyword: str = "") -> str:
    """列出 85 固件版本库下的所有产品目录。

    Args:
        keyword: 过滤关键词（不区分大小写），如 "SRM" 只返回 SRM 开头的产品
    """
    if not _share_reachable():
        return _unreachable_msg()

    try:
        items = sorted(os.listdir(_FW_SHARE))
    except OSError as e:
        return f"❌ 读取产品列表失败: {e}"

    kw = (keyword or "").strip().lower()
    products = [
        e for e in items
        if os.path.isdir(os.path.join(_FW_SHARE, e)) and (not kw or kw in e.lower())
    ]

    if not products:
        if kw:
            return f"ℹ️ 未找到匹配「{keyword}」的产品。"
        return "ℹ️ 固件版本库为空。"

    lines = [f"📦 产品列表（{len(products)} 个" + (f"，匹配「{keyword}」" if kw else "") + "）", ""]
    for p in products:
        lines.append(f"  {p}")
    lines.append("")
    lines.append("💡 调用 browse_product(product=...) 查看子目录。")
    return "\n".join(lines)


def browse_product(product: str) -> str:
    """列出产品下的子目录（不预设目录名，动态列出）。

    Args:
        product: 产品名，如 SRM965
    """
    product = (product or "").strip()
    if not product:
        return "❌ product 参数不能为空。"

    if not _share_reachable():
        return _unreachable_msg()

    product_path = _FW_SHARE / product
    if not product_path.exists():
        return f"❌ 产品目录不存在: {product}"

    try:
        items = sorted(os.listdir(product_path))
    except OSError as e:
        return f"❌ 读取子目录失败: {e}"

    subdirs = [
        e for e in items
        if os.path.isdir(os.path.join(product_path, e))
    ]

    if not subdirs:
        return f"ℹ️ 产品 {product} 下无子目录。"

    lines = [f"📁 {product} 下的子目录（{len(subdirs)} 个）", ""]
    for d in subdirs:
        lines.append(f"  {d}/")
    lines.append("")
    lines.append("💡 调用 list_versions(product=..., subdir=...) 查看版本。")
    return "\n".join(lines)


def list_versions(product: str, subdir: str) -> str:
    """列出版本目录及每个版本下的主固件包。

    Args:
        product: 产品名，如 SRM965
        subdir: 子目录名，从 browse_product 结果获取
    """
    product = (product or "").strip()
    subdir = (subdir or "").strip()
    if not product or not subdir:
        return "❌ product 和 subdir 参数不能为空。"

    if not _share_reachable():
        return _unreachable_msg()

    base_path = _FW_SHARE / product / subdir
    if not base_path.exists():
        return f"❌ 路径不存在: {product}\\{subdir}"

    try:
        items = sorted(os.listdir(base_path))
    except OSError as e:
        return f"❌ 读取版本列表失败: {e}"

    # 分离目录（版本）和文件
    version_dirs = [e for e in items if os.path.isdir(os.path.join(base_path, e))]
    loose_files = [e for e in items if e.lower().endswith(".zip")]

    lines = [f"📋 版本列表：{product}\\{subdir}", ""]

    if version_dirs:
        lines.append(f"版本目录（{len(version_dirs)} 个，按名称排序）：")
        lines.append("-" * 60)
        for vd in version_dirs:
            vd_path = base_path / vd
            # 列出该版本目录下的主固件包
            try:
                vd_items = os.listdir(vd_path)
            except OSError:
                vd_items = []

            main_zip = _find_main_zip(vd_items, vd_path)
            if main_zip:
                sz = os.path.getsize(os.path.join(vd_path, main_zip))
                lines.append(f"  {vd}/  → 主包: {main_zip} ({_fmt_size(sz)})")
            else:
                # 可能是非 zip 格式（散装 flat build 或 .pac）
                fw_files = [
                    f for f in vd_items
                    if f.lower().endswith((".pac", ".xml", ".img", ".elf", ".mbn", ".bin"))
                ]
                if fw_files:
                    lines.append(f"  {vd}/  → 散装固件（{len(fw_files)} 个文件）")
                else:
                    lines.append(f"  {vd}/  → （无可识别固件文件）")
        lines.append("")

    if loose_files:
        lines.append(f"直接存放的 zip（{len(loose_files)} 个）：")
        main_zip = _find_main_zip(loose_files, base_path)
        for f in loose_files:
            sz = os.path.getsize(os.path.join(base_path, f))
            tag = " ← 主包" if f == main_zip else ""
            lines.append(f"  {f} ({_fmt_size(sz)}){tag}")
        lines.append("")

    if not version_dirs and not loose_files:
        return f"ℹ️ {product}\\{subdir} 下无版本目录或固件文件。"

    lines.append("💡 调用 fetch_firmware(product=..., subdir=..., version=...) 拉取解压。")
    return "\n".join(lines)


def _find_main_zip(filenames: list[str], base_path: Path) -> str:
    """从文件名列表中找主固件包（最大且非 _ota/_symbol/_target_files 的 zip）。"""
    candidates = [
        f for f in filenames
        if f.lower().endswith(".zip")
        and not any(f.lower().endswith(s) for s in _EXCLUDE_SUFFIXES)
    ]
    if not candidates:
        return ""
    # 按大小取最大
    best = ""
    best_sz = 0
    for f in candidates:
        try:
            sz = os.path.getsize(os.path.join(base_path, f))
            if sz > best_sz:
                best = f
                best_sz = sz
        except OSError:
            continue
    return best


def fetch_firmware(
    product: str,
    subdir: str,
    version: str,
    local_dir: str = "",
) -> str:
    """从 85 共享拉取固件包，UNC 直接解压到本地目录。

    解压后自动探测 build 类型（meta/flat/pac），返回可直接传给
    qcom_flash(build_path=...) 或 spd_flash(pac_path=...) 的路径。

    Args:
        product: 产品名，如 SRM965
        subdir: 子目录名，从 browse_product 结果获取
        version: 版本目录名，从 list_versions 结果获取
        local_dir: 本地缓存目录，空则用上次保存的偏好
    """
    product = (product or "").strip()
    subdir = (subdir or "").strip()
    version = (version or "").strip()
    if not product or not subdir or not version:
        return "❌ product/subdir/version 参数不能为空。"

    if not _share_reachable():
        return _unreachable_msg()

    version_path = _FW_SHARE / product / subdir / version
    if not version_path.exists():
        return f"❌ 版本路径不存在: {product}\\{subdir}\\{version}"

    # 确定本地缓存目录
    cache_dir = (local_dir or "").strip()
    if not cache_dir:
        cache_dir = _load_cache_dir_pref()
    if not cache_dir:
        return (
            "❌ 未指定本地缓存目录。\n"
            "请通过 local_dir 参数指定（如 D:\\\\fw_cache），或先设置一次后会自动记住。"
        )

    cache_path = Path(cache_dir)
    cache_path.mkdir(parents=True, exist_ok=True)

    # 查找主固件包
    try:
        version_items = os.listdir(version_path)
    except OSError as e:
        return f"❌ 读取版本目录失败: {e}"

    main_zip_name = _find_main_zip(version_items, version_path)
    pac_files = [f for f in version_items if f.lower().endswith(".pac")]

    if main_zip_name:
        return _extract_zip_firmware(
            version_path / main_zip_name, cache_path, product, version
        )
    elif pac_files:
        # 展锐 .pac 文件：直接复制到本地（单文件，通常 < 1GB）
        return _copy_pac_firmware(
            version_path / pac_files[0], cache_path, product, version
        )
    else:
        # 散装 flat build：检查是否有 rawprogram/contents.xml
        has_rawprogram = any("rawprogram" in f.lower() for f in version_items)
        has_contents = any(f.lower() == "contents.xml" for f in version_items)
        if has_rawprogram or has_contents:
            # 散装目录直接可用，复制到本地（避免 UNC 刷机风险）
            return _copy_flat_build(version_path, cache_path, product, version)
        return (
            f"❌ 版本目录 {version} 下未找到主固件包（.zip/.pac/rawprogram*.xml）。\n"
            f"文件列表: {', '.join(version_items[:10])}"
        )


def _extract_zip_firmware(
    zip_path: Path, cache_path: Path, product: str, version: str
) -> str:
    """UNC 路径 zip 直接解压到本地目录，自动探测 build 类型。"""
    zip_sz = zip_path.stat().st_size
    extract_dir = cache_path / f"{product}_{version}"

    # 如果已存在且非空，提示
    if extract_dir.exists() and any(extract_dir.iterdir()):
        return _detect_and_report(extract_dir, product, version, cached=True)

    extract_dir.mkdir(parents=True, exist_ok=True)

    lines = [
        f"📦 拉取固件包: {zip_path.name} ({_fmt_size(zip_sz)})",
        f"📁 解压到: {extract_dir}",
        "⏳ 从 UNC 路径直接解压中（约 1 分钟/GB）...",
        "",
    ]

    try:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)
    except Exception as e:
        # 清理不完整解压
        shutil.rmtree(extract_dir, ignore_errors=True)
        return f"❌ 解压失败: {e}\n已清理不完整文件。"

    # 保存缓存目录偏好
    _save_cache_dir_pref(str(cache_path))

    return _detect_and_report(extract_dir, product, version, cached=False)


def _copy_pac_firmware(
    pac_path: Path, cache_path: Path, product: str, version: str
) -> str:
    """展锐 .pac 文件直接复制到本地。"""
    pac_sz = pac_path.stat().st_size
    dest_dir = cache_path / f"{product}_{version}"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_pac = dest_dir / pac_path.name

    if dest_pac.exists():
        return (
            f"✅ 固件已缓存（跳过复制）\n"
            f"📦 展锐 .pac: {dest_pac} ({_fmt_size(pac_sz)})\n"
            f"💡 传给 spd_flash(pac_path=\"{dest_pac}\") 刷机。"
        )

    try:
        shutil.copy2(pac_path, dest_pac)
    except Exception as e:
        return f"❌ 复制 .pac 失败: {e}"

    _save_cache_dir_pref(str(cache_path))

    return (
        f"✅ 固件包已复制到本地\n"
        f"📦 展锐 .pac: {dest_pac} ({_fmt_size(pac_sz)})\n"
        f"💡 传给 spd_flash(pac_path=\"{dest_pac}\") 刷机。"
    )


def _copy_flat_build(
    src_path: Path, cache_path: Path, product: str, version: str
) -> str:
    """散装 flat build 目录复制到本地（避免 UNC 刷机风险）。"""
    dest_dir = cache_path / f"{product}_{version}"

    if dest_dir.exists() and any(dest_dir.iterdir()):
        return _detect_and_report(dest_dir, product, version, cached=True)

    lines = [
        f"📦 复制散装 flat build: {src_path}",
        f"📁 复制到: {dest_dir}",
        "⏳ 复制中...",
        "",
    ]

    try:
        shutil.copytree(src_path, dest_dir)
    except Exception as e:
        shutil.rmtree(dest_dir, ignore_errors=True)
        return f"❌ 复制 flat build 失败: {e}"

    _save_cache_dir_pref(str(cache_path))

    return _detect_and_report(dest_dir, product, version, cached=False)


def _detect_and_report(
    extract_dir: Path, product: str, version: str, cached: bool
) -> str:
    """探测解压后的 build 类型，返回刷机引导信息。"""
    # 递归查找关键文件
    contents_xml = None
    rawprogram_xml = None
    pac_file = None

    for root, dirs, files in os.walk(extract_dir):
        for f in files:
            fl = f.lower()
            if fl == "contents.xml" and contents_xml is None:
                contents_xml = Path(root) / f
            elif fl.startswith("rawprogram") and fl.endswith(".xml") and rawprogram_xml is None:
                rawprogram_xml = Path(root)  # flat build 目录
            elif fl.endswith(".pac") and pac_file is None:
                pac_file = Path(root) / f

    prefix = "✅ 固件已缓存（跳过）" if cached else "✅ 固件包已解压到本地"
    lines = [prefix, f"📁 目录: {extract_dir}", ""]

    if contents_xml:
        lines.append(f"📦 Build 类型: Meta build（contents.xml）")
        lines.append(f"   路径: {contents_xml}")
        lines.append(
            f"💡 传给 qcom_flash(build_path=\"{contents_xml}\", flavor=\"asic\") 刷机。\n"
            f"   flavor 可选: asic / core_asic"
        )
    elif rawprogram_xml:
        lines.append(f"📦 Build 类型: Flat build（散装镜像）")
        lines.append(f"   路径: {rawprogram_xml}")
        lines.append(
            f"💡 传给 qcom_flash(build_path=\"{rawprogram_xml}\") 刷机。"
        )
    elif pac_file:
        lines.append(f"📦 Build 类型: 展锐 .pac")
        lines.append(f"   路径: {pac_file}")
        lines.append(
            f"💡 传给 spd_flash(pac_path=\"{pac_file}\") 刷机。"
        )
    else:
        lines.append("⚠️ 未识别到刷机元数据文件（contents.xml / rawprogram*.xml / *.pac）")
        lines.append("   请手动检查解压目录。")

    return "\n".join(lines)


def clean_firmware_cache(local_dir: str = "", keep: int = 0) -> str:
    """清理本地固件缓存目录。

    Args:
        local_dir: 缓存目录，空则用上次保存的偏好
        keep: 保留最近 N 个版本目录，0 = 全部清空
    """
    cache_dir = (local_dir or "").strip()
    if not cache_dir:
        cache_dir = _load_cache_dir_pref()
    if not cache_dir:
        return "❌ 未指定缓存目录，且无上次保存的偏好。"

    cache_path = Path(cache_dir)
    if not cache_path.exists():
        return f"❌ 缓存目录不存在: {cache_dir}"

    # 列出所有子目录（版本缓存），按修改时间排序
    subdirs = [
        (d, d.stat().st_mtime)
        for d in cache_path.iterdir()
        if d.is_dir()
    ]
    subdirs.sort(key=lambda x: x[1], reverse=True)  # 最新在前

    if not subdirs:
        return f"ℹ️ 缓存目录 {cache_dir} 为空，无需清理。"

    if keep >= len(subdirs):
        return f"ℹ️ 只有 {len(subdirs)} 个目录，keep={keep} 全部保留。"

    to_remove = subdirs[keep:]  # 删除 keep 之后的
    removed_size = 0
    removed_count = 0
    failed = 0

    for d, _ in to_remove:
        try:
            sz = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            shutil.rmtree(d)
            removed_size += sz
            removed_count += 1
        except Exception:
            failed += 1

    kept = subdirs[:keep]
    lines = [
        f"🧹 清理固件缓存: {cache_dir}",
        f"   删除: {removed_count} 个目录 ({_fmt_size(removed_size)})",
    ]
    if failed:
        lines.append(f"   ⚠️ {failed} 个目录删除失败")
    if kept:
        lines.append(f"   保留: {len(kept)} 个最新目录")
        for d, _ in kept:
            lines.append(f"     {d.name}")
    lines.append("✅ 清理完成。")
    return "\n".join(lines)
