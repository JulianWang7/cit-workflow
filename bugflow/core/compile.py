"""Android 编译层 — make 单模块增量编译。

用 make <module_name> -j32（不用 mmm）：mmm 对 -j 32 的空格解析有坑
（会把 32 当目录参数），make 无此问题。

命令拼法（自包含）：
  - cd <build_dir> && source build/envsetup.sh && lunch <variant> && make <module_name> -j32
  - Qualcomm 3 段：build_dir = root/vendor_seg（如需）
  - shlex.quote 所有用户输入（module/variant）
  - 产物 find out/target/product

后台模式（background=True）：长编译提交到后台，返回 task_id，
agent 用 build_status() 轮询，SSH 断连不丢编译。
"""

from __future__ import annotations

import logging
import re
import shlex

from bugflow.core.ssh import ssh_exec, SSHError
from bugflow.core.tasks import (
    submit_task,
    check_task,
    list_tasks,
    stop_task,
)

logger = logging.getLogger("bugflow.core.compile")

# Android 构建失败标记（make/ninja 格式），比泛搜 "FAILED"/"error:" 精确
_BUILD_FAIL_MARKERS = ("make: ***", "ninja: build stopped", "FAILED:")

# 产品名 → lunch variant 前缀映射表。
# 组合规则: {prefix}-{ro.build.type}，如 SRM965 + userdebug → qssi_arm64-userdebug
# 用户可在 ~/.bugfix-flow/servers.yaml 扩展（config 模块会 merge）。
VARIANT_MAP: dict[str, str] = {
    # Qualcomm 平台
    "SRM965": "qssi_arm64",
    "SRM965_A16": "qssi_arm64",
    "SRM963": "qssi_arm64",
    "SRM965_LXD": "qssi_arm64",
    # Unisoc 平台 — 产品名即 variant 前缀
    "uis7870_3h10_car": "uis7870_3h10_car",
    "uis7870_3h10_car_native": "uis7870_3h10_car_native",
}


def detect_module_name(server: str, code_root: str, module: str) -> str:
    """从 Android.bp/Android.mk 自动检测模块名。

    module 可以是路径（如 packages/apps/Settings）或已知的模块名。
    如果 module 含 "/"，在 code_root/module/ 下搜 Android.bp → Android.mk；
    如果不含 "/"，直接当模块名返回（用户已显式指定）。

    Args:
        server: SSH 服务器名
        code_root: Android 源码树根
        module: 模块路径或模块名

    Returns:
        检测到的模块名（如 gps.polaris），找不到则 fallback 到目录名。
    """
    # 不含 "/" → 用户已指定模块名，直接返回
    if "/" not in module:
        return module.strip()

    module_path = module.strip().rstrip("/")
    dir_name = module_path.split("/")[-1]

    # 先搜 Android.bp（Soong），再搜 Android.mk（Make）
    for build_file, patterns in (
        ("Android.bp", [r'name:\s*"([^"]+)"', r"name:\s*'([^']+)'"]),
        ("Android.mk", [r'LOCAL_MODULE\s*:=\s*(\S+)']),
    ):
        full_path = f"{code_root}/{module_path}/{build_file}"
        try:
            content, _, ec = ssh_exec(server, f"cat {shlex.quote(full_path)}", timeout=10)
        except SSHError:
            continue
        if ec != 0 or not content.strip():
            continue
        for pat in patterns:
            matches = re.findall(pat, content)
            if matches:
                # 取第一个匹配（主模块名）
                detected = matches[0].strip()
                logger.info("自动检测模块名: %s/%s → %s", module_path, build_file, detected)
                return detected

    # fallback: 目录名（原逻辑）
    logger.warning("未在 %s 找到 Android.bp/Android.mk 模块名，fallback 到目录名: %s", module_path, dir_name)
    return dir_name


def detect_variant(serial: str = "") -> str:
    """从设备 getprop 自动推断 lunch variant。

    读取 ro.build.product（产品名）和 ro.build.type（构建类型），
    通过 VARIANT_MAP 映射产品名到 variant 前缀，组合为 {prefix}-{type}。

    Args:
        serial: 设备序列号，空则自动选择

    Returns:
        推断的 variant（如 "qssi_arm64-userdebug"），找不到映射则返回 ""。
    """
    # 延迟导入避免循环依赖
    from bugflow.core.adb import adb_shell, resolve_device

    try:
        ser = resolve_device(serial) if not serial else serial
    except Exception:
        return ""

    try:
        product_out, _, p_ec = adb_shell("getprop ro.build.product", ser, 5)
        type_out, _, t_ec = adb_shell("getprop ro.build.type", ser, 5)
    except Exception as e:
        logger.warning("detect_variant getprop 失败: %s", e)
        return ""

    product = product_out.strip()
    build_type = type_out.strip() if t_ec == 0 else "userdebug"

    if not product:
        return ""

    prefix = VARIANT_MAP.get(product, "")
    if not prefix:
        # 模糊匹配：产品名以已知 key 为前缀（如 SRM965_A16 → SRM965）
        # P3-10: 只允许 product.startswith(key)，禁止反向 key.startswith(product) —
        # 反向会让 "SRM" 匹配 "SRM965" 导致误匹配；len(key)>=4 防短名误匹配
        for key, val in VARIANT_MAP.items():
            if product.startswith(key) and len(key) >= 4:
                prefix = val
                break

    if not prefix:
        logger.warning("VARIANT_MAP 无产品 %s 的映射，请更新 core/compile.py VARIANT_MAP", product)
        return ""

    variant = f"{prefix}-{build_type}"
    logger.info("自动推断 variant: product=%s type=%s → %s", product, build_type, variant)
    return variant


def detect_variant_from_build(server: str, code_root: str) -> str:
    """从构建产物推断 lunch variant（不依赖设备连接）。

    编译时设备不一定在线，detect_variant() 读 getprop 会失败。
    此函数从 out/ 目录的构建产物推断:

    1. ls out/build-*.ninja → 文件名含 product 名
       如 out/build-uis7870_3h10_car_native.ninja → product=uis7870_3h10_car_native
    2. cat out/target/product/*/build_fingerprint.txt → 解析 build type
       fingerprint 格式: brand/product/device:release/id/incremental:type/tags
       按 : 分隔共 3 段，build type 在第 3 段（parts[2]）的 / 前半部分
    3. 组合 {product}-{type}

    Args:
        server: SSH 服务器名
        code_root: Android 源码树根

    Returns:
        推断的 variant（如 "uis7870_3h10_car_native-eng"），找不到返回 ""
    """
    try:
        ninja_out, _, ec = ssh_exec(
            server, f"ls {shlex.quote(code_root)}/out/build-*.ninja 2>/dev/null | head -1",
            timeout=10,
        )
    except SSHError as e:
        logger.warning("detect_variant_from_build: SSH 失败: %s", e)
        return ""

    if ec != 0 or not ninja_out.strip():
        logger.warning("detect_variant_from_build: 无 out/build-*.ninja 文件")
        return ""

    ninja_file = ninja_out.strip().split("\n")[0].strip()
    # out/build-uis7870_3h10_car_native.ninja → uis7870_3h10_car_native
    basename = ninja_file.rsplit("/", 1)[-1]
    product = basename.replace("build-", "").replace(".ninja", "")

    if not product:
        logger.warning("detect_variant_from_build: 无法从 %s 解析 product 名", ninja_file)
        return ""

    # 从 build_fingerprint.txt 解析 build type
    build_type = "userdebug"  # 安全默认值
    try:
        fp_out, _, fp_ec = ssh_exec(
            server,
            f"cat {shlex.quote(code_root)}/out/target/product/*/build_fingerprint.txt 2>/dev/null | head -1",
            timeout=10,
        )
        if fp_ec == 0 and fp_out.strip():
            # Android fingerprint 格式:
            #   brand/product/device:release/id/incremental:type/tags
            # → 3 个冒号分隔段，build type 在第 3 段的 / 前半部分
            parts = fp_out.strip().split(":")
            if len(parts) >= 3:
                candidate = parts[2].split("/")[0].strip()
                if candidate in ("eng", "userdebug", "user"):
                    build_type = candidate
    except SSHError:
        pass  # fallback 到 userdebug

    variant = f"{product}-{build_type}"
    logger.info("detect_variant_from_build: ninja=%s → product=%s type=%s → %s",
                basename, product, build_type, variant)
    return variant


def build_module(
    server: str,
    code_root: str,
    module: str,
    variant: str = "",
    vendor_seg: str = "",
    timeout: int = 600,
    background: bool = False,
    clean_mode: str = "",
    prebuilt_out: str = "",
) -> dict:
    """make 单模块增量编译。

    用 make <module_name>（不用 mmm）：mmm 对 -j 32 的空格解析有坑
   （会把 32 当目录参数），make 无此问题。

    Args:
        server: SSH 服务器名
        code_root: Android 源码树根（如 /home7/yangwei/SRM965）
        module: 模块路径（如 packages/apps/Settings）或模块名（如 Settings）
        variant: lunch 目标，空则自动检测（out/build-*.ninja → getprop fallback）
        vendor_seg: Qualcomm 3 段 vendor 目录名（空则用 code_root 编译）
        timeout: 编译超时秒（background=True 时忽略）
        background: True → 后台提交返回 task_id；False → 阻塞等待
        clean_mode: 编译前清理力度（默认 "" 不清理，纯增量最快）:
            "" — 不碰 out，直接 make（90% 场景）
            "installclean" — make installclean，清 image 产物保留中间 .o
            "restore" — rm -rf out && cp -a <prebuilt_out> out，需配 prebuilt_out
        prebuilt_out: clean_mode="restore" 时指定的预构建 out 目录路径

    Returns:
        background=True: {success, background, task_id, status, message}
        background=False: {success, exit_code, output, artifacts, error?}
    """
    if not module:
        return {"success": False, "exit_code": -1, "output": "", "artifacts": "",
                "error": "未指定 module"}

    build_dir = code_root
    vendor_warning = ""
    if vendor_seg:
        build_dir = f"{code_root}/{vendor_seg}"
        # VENDOR 树直接 make 可能因 soong-clang-prebuilts 重复定义失败
        vendor_warning = (
            "⚠️ VENDOR 树直接 make 可能失败（soong-clang-prebuilts 重复定义），"
            "建议改用 compile_script_build（平台整编脚本内含 prepare_vendor 流程）"
        )
        logger.warning("build_module: %s", vendor_warning)

    # variant 自动检测: 构建产物 → 设备 getprop → 报错
    if not variant:
        variant = detect_variant_from_build(server, code_root)
        if not variant:
            variant = detect_variant()
        if not variant:
            return {"success": False, "exit_code": -1, "output": "", "artifacts": "",
                    "error": "无法自动检测 variant。请手动指定，或检查 out/build-*.ninja"}
        logger.info("build_module: 自动检测 variant=%s", variant)

    # 从 Android.bp/Android.mk 自动检测模块名（目录名可能 ≠ 模块名）
    module_name = detect_module_name(server, code_root, module)

    # 编译前清理（按力度选，默认不碰 out）
    # P0-2: installclean 需要 envsetup+lunch 环境，必须放在 lunch 之后
    # restore 只是 rm+cp，不需要环境，放在 cd 之后即可
    pre_env_cmd = ""
    post_lunch_cmd = ""
    if clean_mode == "installclean":
        post_lunch_cmd = "make installclean && "
    elif clean_mode == "restore":
        if not prebuilt_out:
            return {"success": False, "exit_code": -1, "output": "", "artifacts": "",
                    "error": "clean_mode=restore 需要 prebuilt_out 参数指定预构建 out 目录"}
        pre_env_cmd = f"rm -rf out && cp -a {shlex.quote(prebuilt_out)} out && "

    # shlex.quote 所有自由文本（踩坑 5）
    cmd = (
        f"cd {shlex.quote(build_dir)} && "
        f"{pre_env_cmd}"
        f"source build/envsetup.sh && "
        f"lunch {shlex.quote(variant)} && "
        f"{post_lunch_cmd}"
        f"make {shlex.quote(module_name)} -j32"
    )

    # ── 后台模式：提交到后台，立即返回 task_id ──
    # P2-7: 传检测到的 module_name（非原始路径）到 task.module，
    # build_status → _find_artifacts 用此名查找产物
    if background:
        try:
            task = submit_task(server, cmd, module=module_name, code_root=code_root)
        except RuntimeError as e:
            return {"success": False, "background": True, "task_id": "",
                    "status": "failed", "message": str(e)}
        return {
            "success": True,
            "background": True,
            "task_id": task.task_id,
            "status": "running",
            "warning": vendor_warning,
            "message": (
                f"编译已后台提交 (task_id={task.task_id}, pid={task.pid})。"
                f"用 build_status(task_id='{task.task_id}') 轮询进度。"
            ),
        }

    # ── 阻塞模式：原逻辑 ──
    try:
        out, err, ec = ssh_exec(server, cmd, timeout=timeout)
    except SSHError as e:
        err_msg = str(e)
        if "timeout" in err_msg.lower() or "timed out" in err_msg.lower():
            err_msg += "（SSH 超时 — 编译可能仍在服务器运行，建议用 background=True 或增大 timeout）"
        return {"success": False, "exit_code": -1, "output": "",
                "error": err_msg}

    result = out + ("\n" + err if err.strip() else "")
    success = ec == 0 and not any(m in result for m in _BUILD_FAIL_MARKERS)

    artifacts = ""
    if success:
        artifacts = _find_artifacts(server, code_root, module_name)

    return {
        "success": success,
        "exit_code": ec,
        "output": result,
        "artifacts": artifacts,
        "warning": vendor_warning,
        "error": "" if success else f"编译失败 (exit={ec})",
    }


def build_full(
    server: str,
    code_root: str,
    variant: str = "",
    vendor_seg: str = "",
    timeout: int = 600,
    background: bool = True,
) -> dict:
    """全树编译（整编）。不指定模块，make -j32 编译整个代码树。

    用于批量修复完成后将所有模块改动编译到一个完整镜像。

    variant 自动推断：variant 为空时从设备 getprop 读取。

    Args:
        server: SSH 服务器名
        code_root: Android 源码树根
        variant: lunch 目标，空=自动从设备推断
        vendor_seg: Qualcomm 3 段 vendor 目录名（空则用 code_root 编译）
        timeout: 编译超时秒（background=True 时忽略）
        background: True → 后台提交返回 task_id；False → 阻塞等待

    Returns:
        background=True: {success, background, task_id, status, message}
        background=False: {success, exit_code, output, error?}
    """
    build_dir = code_root
    if vendor_seg:
        build_dir = f"{code_root}/{vendor_seg}"

    # P0-1: variant 自动检测链 — 构建产物 → 设备 getprop → 报错
    # 原代码 detect_variant() or "qssi_arm64-userdebug" 硬编码高通默认，
    # 展锐树会失败；复用 build_module 同样的两段检测
    if not variant:
        variant = detect_variant_from_build(server, code_root)
        if not variant:
            variant = detect_variant()
        if not variant:
            return {"success": False, "exit_code": -1, "output": "",
                    "error": "无法自动检测 variant。请手动指定，或检查 out/build-*.ninja"}
        logger.info("build_full: 自动检测 variant=%s", variant)

    cmd = (
        f"cd {shlex.quote(build_dir)} && "
        f"source build/envsetup.sh && "
        f"lunch {shlex.quote(variant)} && "
        f"make -j32"
    )

    # ── 后台模式 ──
    if background:
        try:
            task = submit_task(server, cmd, module="full_build", code_root=code_root)
        except RuntimeError as e:
            return {"success": False, "background": True, "task_id": "",
                    "status": "failed", "message": str(e)}
        return {
            "success": True,
            "background": True,
            "task_id": task.task_id,
            "status": "running",
            "message": (
                f"整编已后台提交 (task_id={task.task_id}, pid={task.pid})。"
                f"用 build_status(task_id='{task.task_id}') 轮询进度。"
            ),
        }

    # ── 阻塞模式 ──
    try:
        out, err, ec = ssh_exec(server, cmd, timeout=timeout)
    except SSHError as e:
        err_msg = str(e)
        if "timeout" in err_msg.lower() or "timed out" in err_msg.lower():
            err_msg += "（SSH 超时 — 编译可能仍在服务器运行，建议用 background=True 或增大 timeout）"
        return {"success": False, "exit_code": -1, "output": "",
                "error": err_msg}

    result = out + ("\n" + err if err.strip() else "")
    success = ec == 0 and not any(m in result for m in _BUILD_FAIL_MARKERS)

    return {
        "success": success,
        "exit_code": ec,
        "output": result[-3000:],
        "error": "" if success else f"整编失败 (exit={ec})",
    }


def _find_artifacts(server: str, code_root: str, module_name: str) -> str:
    """编译成功后找产物（APK/SO/JAR）。

    Args:
        module_name: 已检测到的模块名（如 SettingsProvider），非目录路径。
                     如果传入路径，取最后一段作为 fallback。
    """
    name = module_name.split("/")[-1] if "/" in module_name else module_name
    name_glob = shlex.quote(f"{name}*")
    find_cmd = (
        f"find {shlex.quote(code_root)}/out/target/product "
        f"-name {name_glob} -type f 2>/dev/null | head -10"
    )
    try:
        find_out, _, _ = ssh_exec(server, find_cmd, timeout=15)
        return find_out.strip()
    except SSHError:
        return ""  # 产物查找失败不影响编译成功判定


def build_status(task_id: str, code_root: str = "") -> dict:
    """轮询后台编译状态。成功时自动查产物。

    Args:
        task_id: submit_task 返回的 task_id
        code_root: 源码树根（成功时查产物用，空则跳过产物查找）

    Returns:
        {task_id, status, exit_code, log_tail, elapsed, module, artifacts?}
    """
    result = check_task(task_id)

    # 编译完成 → 查产物
    if result["status"] == "completed" and code_root and result["module"]:
        server = _task_server(task_id)
        if server:
            result["artifacts"] = _find_artifacts(server, code_root, result["module"])

    return result


def build_list() -> str:
    """列出所有编译任务（格式化输出）。"""
    tasks = list_tasks()
    if not tasks:
        return "(无编译任务)"
    lines = []
    for t in tasks:
        elapsed = f"{t['elapsed']:.0f}s" if t["elapsed"] is not None else "-"
        icon = {"running": "⏳", "completed": "✅", "failed": "❌",
                "stopped": "🛑"}.get(t["status"], "?")
        lines.append(
            f"  {icon} {t['task_id']}  [{t['status']}]  {t['module']}  {elapsed}"
        )
    return f"编译任务 ({len(tasks)}):\n" + "\n".join(lines)


def build_stop(task_id: str) -> str:
    """停止后台编译任务。"""
    result = stop_task(task_id)
    if result.get("status") == "not_found":
        return f"任务不存在: {task_id}"
    return f"已停止: {task_id}"


def _task_server(task_id: str) -> str:
    """从 task_id 查 server（内部用）。"""
    from bugflow.core.tasks import _tasks, _tasks_lock
    with _tasks_lock:
        task = _tasks.get(task_id)
    return task.server if task else ""


def find_artifacts(server: str, code_root: str, name_pattern: str, timeout: int = 15) -> str:
    """在 out/target/product 下找编译产物。name_pattern 支持 glob。"""
    name_glob = shlex.quote(name_pattern)
    cmd = (
        f"find {shlex.quote(code_root)}/out/target/product "
        f"-name {name_glob} -type f 2>/dev/null | head -20"
    )
    try:
        out, _, _ = ssh_exec(server, cmd, timeout=timeout)
        return out.strip()
    except SSHError as e:
        return f"(查找失败: {e})"


# ── 平台整编脚本 ──────────────────────────────────────────────
# 每套代码根目录下都有维护好的整编脚本：
#   展锐: code_root/*.sh             （srm936_meig_userdebug.sh 等）
#   高通: code_root/LA.VENDOR.*/*.sh （SRM965-Vehicle_build_A16.sh 等）
# 插件不重新实现编译逻辑，只负责：发现脚本 → 解析参数 → 构造命令 → 后台执行

# 展锐脚本文件名关键词（过滤掉 build/envsetup.sh 等无关脚本）
_SPRD_SCRIPT_HINTS = ("meig", "userdebug")

# 高通脚本排除列表（LA.VENDOR 下有很多非整编的 .sh）
# 精确匹配: build.sh / envsetup.sh; 前缀匹配: clean*
_QCOM_SCRIPT_EXCLUDES_EXACT = ("build.sh", "envsetup.sh")
_QCOM_SCRIPT_EXCLUDES_PREFIX = ("clean", "run_unit", "test_defs")

# 高通支持的编译模式（从脚本 case 语句提取）
_QCOM_MODES = ("qssi", "le", "target", "ota", "super")


def detect_build_scripts(server: str, code_root: str) -> list[dict]:
    """自动发现 code_root 下的平台整编脚本。

    搜索策略:
      - 展锐: ls code_root/*.sh → 过滤文件名含 meig/userdebug
      - 高通: ls code_root/LA.VENDOR.*/*.sh → 过滤含 build 且排除通用脚本

    Returns:
        [{"script_path", "script_dir", "platform", "filename"}, ...]
        platform 为 "sprd" 或 "qcom"
    """
    results: list[dict] = []

    # 展锐: 根目录 *.sh
    try:
        out, _, ec = ssh_exec(
            server, f"ls {shlex.quote(code_root)}/*.sh 2>/dev/null",
            timeout=10,
        )
        if ec == 0:
            for line in out.strip().splitlines():
                path = line.strip()
                if not path:
                    continue
                filename = path.rsplit("/", 1)[-1]
                if any(h in filename.lower() for h in _SPRD_SCRIPT_HINTS):
                    results.append({
                        "script_path": path,
                        "script_dir": path.rsplit("/", 1)[0] + "/",
                        "platform": "sprd",
                        "filename": filename,
                    })
    except SSHError as e:
        logger.debug("detect_build_scripts: 展锐搜索失败: %s", e)

    # 高通: LA.VENDOR.*/*.sh
    try:
        out, _, ec = ssh_exec(
            server,
            f"ls {shlex.quote(code_root)}/LA.VENDOR.*/*.sh 2>/dev/null",
            timeout=10,
        )
        if ec == 0:
            for line in out.strip().splitlines():
                path = line.strip()
                if not path:
                    continue
                filename = path.rsplit("/", 1)[-1]
                # 排除通用脚本（精确匹配 + 前缀匹配，避免 build.sh 误杀 *_build.sh）
                fname_lower = filename.lower()
                if fname_lower in _QCOM_SCRIPT_EXCLUDES_EXACT:
                    continue
                if any(fname_lower.startswith(ex) for ex in _QCOM_SCRIPT_EXCLUDES_PREFIX):
                    continue
                # 只保留含 build 的（整编脚本命名规律）
                if "build" not in filename.lower():
                    continue
                results.append({
                    "script_path": path,
                    "script_dir": path.rsplit("/", 1)[0] + "/",
                    "platform": "qcom",
                    "filename": filename,
                })
    except SSHError as e:
        logger.debug("detect_build_scripts: 高通搜索失败: %s", e)

    logger.info("detect_build_scripts: 发现 %d 个脚本", len(results))
    return results


def parse_build_script(server: str, script_path: str) -> dict:
    """读取完整脚本，正则提取参数信息。

    Returns:
        {
            "platform": "sprd"|"qcom"|"unknown",
            "boards": list[str],           # 高通板级，展锐为空
            "build_types": list[str],      # user/userdebug
            "default_build_num": str,      # 脚本内 BUILDNUM[1]
            "modes": list[str],            # 高通编译模式，展锐为空
        }
    """
    info: dict = {
        "platform": "unknown",
        "boards": [],
        "build_types": [],
        "default_build_num": "",
        "modes": [],
    }

    try:
        # P2-8: 用 cat 读全文替代 head -300 — 长 case 语句可能超 300 行
        content, _, ec = ssh_exec(
            server, f"cat {shlex.quote(script_path)}", timeout=10,
        )
        if ec != 0 or not content.strip():
            return info
    except SSHError as e:
        logger.warning("parse_build_script: 读取失败 %s: %s", script_path, e)
        return info

    # 判断平台：展锐用 choosecombo，高通用 lunch <board>-<type>
    if "choosecombo" in content:
        info["platform"] = "sprd"
    elif "lunch" in content and "MDBRANCH" in content:
        info["platform"] = "qcom"

    # 提取 build_types: DEBUGARR[1]="user" / DEBUGARR[2]="userdebug"
    type_matches = re.findall(r'DEBUGARR\[\d+\]="([^"]+)"', content)
    if type_matches:
        info["build_types"] = type_matches

    # 提取 boards: MDBRANCH[1]="kalama"
    board_matches = re.findall(r'MDBRANCH\[\d+\]="([^"]+)"', content)
    if board_matches:
        info["boards"] = board_matches

    # 提取 default_build_num: BUILDNUM[1]="26"
    num_match = re.search(r'BUILDNUM\[\d+\]="([^"]+)"', content)
    if num_match:
        info["default_build_num"] = num_match.group(1)

    # 高通: 从 case 语句提取编译模式
    if info["platform"] == "qcom":
        for mode in _QCOM_MODES:
            if f"--{mode})" in content:
                info["modes"].append(mode)
        if not info["modes"]:
            info["modes"] = ["full"]  # 至少支持全编

    logger.info("parse_build_script: %s → %s", script_path.rsplit("/", 1)[-1], info)
    return info


def build_script(
    server: str,
    code_root: str,
    script: str = "",
    build_type: str = "userdebug",
    build_num: str = "",
    board: str = "",
    mode: str = "",
    ota: bool = False,
    clean: bool = False,
    background: bool = True,
    timeout: int = 600,
) -> dict:
    """调用平台整编脚本编译完整镜像。

    自动检测展锐/高通脚本，按平台类型构造命令参数，复用后台任务系统。

    Args:
        server: SSH 服务器名
        code_root: Android 源码树根
        script: 脚本路径，空=自动检测
        build_type: 编译类型 user/userdebug
        build_num: 版本号，空=用脚本默认
        board: 高通板级（kalama/canoe/mc5612），空=用脚本第一个
        mode: 高通编译模式 qssi/le/target/ota/super，空=full
        ota: 展锐是否打 OTA 包
        clean: 高通 --clean 编译前清理
        background: True → 后台提交返回 task_id
        timeout: 阻塞模式超时秒

    Returns:
        background=True: {success, background, task_id, status, message}
        background=False: {success, exit_code, output, error?}
        多脚本/无脚本: {success=False, scripts?, error}
    """
    # ── 1. 脚本检测 ──
    if not script:
        scripts = detect_build_scripts(server, code_root)
        if not scripts:
            return {"success": False, "error": f"未在 {code_root} 找到整编脚本"}
        if len(scripts) > 1:
            # 多脚本 → 返回列表让用户选
            lines = [f"检测到 {len(scripts)} 个整编脚本，请指定 script 参数:"]
            for i, s in enumerate(scripts):
                lines.append(
                    f"  [{i}] [{s['platform']}] {s['filename']}"
                )
                lines.append(f"      路径: {s['script_path']}")
            lines.append(f"\n示例: compile_script_build(script='{scripts[0]['filename']}')")
            return {"success": False, "scripts": scripts, "error": "\n".join(lines)}
        script_info = scripts[0]
    else:
        # 用户指定了脚本，构造 script_info
        script_path = script if script.startswith("/") else f"{code_root}/{script}"
        script_info = {
            "script_path": script_path,
            "script_dir": script_path.rsplit("/", 1)[0] + "/",
            "platform": "unknown",
            "filename": script_path.rsplit("/", 1)[-1],
        }

    script_path = script_info["script_path"]
    script_dir = script_info["script_dir"]

    # ── 2. 解析脚本参数 ──
    parsed = parse_build_script(server, script_path)
    platform = parsed["platform"]
    script_info["platform"] = platform

    # ── 3. 参数补全 ──
    if not build_num:
        build_num = parsed.get("default_build_num", "")

    if platform == "qcom":
        if not board and parsed["boards"]:
            board = parsed["boards"][0]
        if not mode:
            mode = "full"

    # ── 4. 构造命令（按平台类型）──
    if platform == "sprd":
        # 展锐: bash <script> <build_type> [ota] [build_num]
        args = [build_type]
        if ota:
            args.append("ota")
        if build_num:
            args.append(build_num)
        args_str = " ".join(shlex.quote(a) for a in args)
    elif platform == "qcom":
        # 高通: bash <script> <build_type> <board> <build_num> [--mode] [--clean]
        args = [build_type, board, build_num]
        if mode and mode != "full":
            args.append(f"--{mode}")
        if clean:
            args.append("--clean")
        args_str = " ".join(shlex.quote(a) for a in args if a)
    else:
        # 平台未知 → 展锐格式尝试（最简参数）
        logger.warning("build_script: 无法识别脚本平台，用展锐格式")
        args = [build_type]
        if ota:
            args.append("ota")
        if build_num:
            args.append(build_num)
        args_str = " ".join(shlex.quote(a) for a in args)

    cmd = f"cd {shlex.quote(script_dir)} && bash {shlex.quote(script_path)} {args_str}"
    module_tag = f"script_build:{script_info['filename']}"

    logger.info("build_script: %s", cmd)

    # ── 5. 后台/阻塞执行 ──
    if background:
        try:
            task = submit_task(server, cmd, module=module_tag, code_root=code_root)
        except RuntimeError as e:
            return {"success": False, "background": True, "task_id": "",
                    "status": "failed", "message": str(e)}
        return {
            "success": True,
            "background": True,
            "task_id": task.task_id,
            "status": "running",
            "platform": platform,
            "script": script_info["filename"],
            "message": (
                f"整编已后台提交 (task_id={task.task_id}, pid={task.pid})。\n"
                f"平台: {platform}, 脚本: {script_info['filename']}\n"
                f"用 compile_status(task_id='{task.task_id}') 轮询进度。"
            ),
        }

    # 阻塞模式
    try:
        out, err, ec = ssh_exec(server, cmd, timeout=timeout)
    except SSHError as e:
        err_msg = str(e)
        if "timeout" in err_msg.lower() or "timed out" in err_msg.lower():
            err_msg += "（SSH 超时 — 整编可能仍在服务器运行，建议用 background=True）"
        return {"success": False, "exit_code": -1, "output": "", "error": err_msg}

    result = out + ("\n" + err if err.strip() else "")
    success = ec == 0 and not any(m in result for m in _BUILD_FAIL_MARKERS)

    return {
        "success": success,
        "exit_code": ec,
        "platform": platform,
        "script": script_info["filename"],
        "output": result[-3000:],
        "error": "" if success else f"整编失败 (exit={ec})",
    }
