"""bugfix-setup — 首次配置检查向导。

逐项检查 android-bugfix-flow 的所有依赖是否就绪，缺失项给出修复指引。
支持 --fix 自动复制模板文件到 ~/.bugfix-flow/。

Usage:
    bugfix-setup           # 检查所有依赖
    bugfix-setup --fix     # 检查 + 自动复制缺失的模板文件
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


# ── 检查结果 ──────────────────────────────────────────────────
@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    fix_hint: str = ""


# ── 模板文件查找 ──────────────────────────────────────────────
def _templates_dir() -> Path:
    """config/ 模板目录（相对于包根或当前工作目录）。"""
    # 尝试从包安装位置找
    here = Path(__file__).resolve().parent.parent.parent  # bugflow/cli/setup.py → repo root
    config_dir = here / "config"
    if config_dir.is_dir():
        return config_dir
    # 回退：当前工作目录
    return Path.cwd() / "config"


def _config_dir() -> Path:
    """用户配置目录 ~/.bugfix-flow/。"""
    env = os.environ.get("BUGFIX_CONFIG_DIR")
    if env:
        return Path(os.path.expanduser(env))
    return Path.home() / ".bugfix-flow"


# ── 检查函数 ──────────────────────────────────────────────────
def check_config_dir() -> CheckResult:
    """检查配置目录是否存在。"""
    d = _config_dir()
    if d.is_dir():
        return CheckResult("配置目录", True, str(d))
    return CheckResult(
        "配置目录", False, f"~/.bugfix-flow/ 不存在",
        f"mkdir -p {d}  或运行 bugfix-setup --fix",
    )


def check_bugflow_install() -> CheckResult:
    """检查 bugflow pip 包是否安装。"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", "import bugflow; print(bugflow.__file__)"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            return CheckResult("bugflow 安装", True, result.stdout.strip())
        return CheckResult(
            "bugflow 安装", False, "import bugflow 失败",
            "cd <repo-root> && pip install -e '.[mcp]'",
        )
    except Exception as e:
        return CheckResult("bugflow 安装", False, str(e), "pip install -e '.[mcp]'")


def check_servers_yaml() -> CheckResult:
    """检查 servers.yaml 是否存在且有有效服务器。"""
    p = _config_dir() / "servers.yaml"
    if not p.is_file():
        return CheckResult(
            "SSH 配置", False, "servers.yaml 不存在",
            f"cp config/servers.yaml.example {p}  然后编辑填自己的 SSH 信息",
        )
    try:
        import yaml
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        servers = data.get("servers", {}) if isinstance(data, dict) else {}
        if not servers:
            return CheckResult(
                "SSH 配置", False, "servers.yaml 无服务器条目",
                f"编辑 {p}，参考 config/servers.yaml.example 添加服务器",
            )
        names = list(servers.keys())
        return CheckResult("SSH 配置", True, f"{len(names)} 个服务器: {', '.join(names)}")
    except Exception as e:
        return CheckResult(
            "SSH 配置", False, f"解析失败: {e}",
            f"检查 {p} 格式，参考 config/servers.yaml.example",
        )


def check_zentao_yaml() -> CheckResult:
    """检查 zentao.yaml 是否存在且配置完整。"""
    p = _config_dir() / "zentao.yaml"
    if not p.is_file():
        return CheckResult(
            "禅道配置", False, "zentao.yaml 不存在",
            f"cp config/zentao.yaml.example {p}  然后编辑填禅道凭据",
        )
    try:
        import yaml
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        zt = data.get("zentao", {}) if isinstance(data, dict) else {}
        required = ["base_url", "user", "password", "mcp_token", "mcp_secret"]
        missing = [k for k in required if not zt.get(k)]
        if missing:
            return CheckResult(
                "禅道配置", False, f"缺失字段: {', '.join(missing)}",
                f"编辑 {p}，填入禅道凭据（参考 config/zentao.yaml.example）",
            )
        return CheckResult("禅道配置", True, f"base_url={zt.get('base_url')}")
    except Exception as e:
        return CheckResult(
            "禅道配置", False, f"解析失败: {e}",
            f"检查 {p} 格式，参考 config/zentao.yaml.example",
        )


def check_llm_yaml() -> CheckResult:
    """检查 llm.yaml 是否存在（可选配置，缺失只是回退到 ZCode config.json）。"""
    p = _config_dir() / "llm.yaml"
    if not p.is_file():
        # llm.yaml 是可选的，回退到 ZCode config.json
        zcode_config = Path.home() / ".zcode" / "v2" / "config.json"
        if zcode_config.is_file():
            return CheckResult(
                "LLM 配置", True,
                "llm.yaml 不存在，回退到 ~/.zcode/v2/config.json",
                f"（可选）cp config/llm.yaml.example {p}  配置专用 LLM",
            )
        return CheckResult(
            "LLM 配置", False, "llm.yaml 和 ZCode config.json 都不存在",
            f"cp config/llm.yaml.example {p}  或在 ZCode 中配置 provider",
        )
    try:
        import yaml
        with p.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        llm = data.get("llm", {}) if isinstance(data, dict) else {}
        required = ["model", "base_url", "api_key"]
        missing = [k for k in required if not llm.get(k)]
        if missing:
            return CheckResult(
                "LLM 配置", False, f"缺失字段: {', '.join(missing)}",
                f"编辑 {p}，填入 LLM 配置",
            )
        return CheckResult("LLM 配置", True, f"model={llm.get('model')}")
    except Exception as e:
        return CheckResult(
            "LLM 配置", False, f"解析失败: {e}",
            f"检查 {p} 格式",
        )


def check_search_tools() -> CheckResult:
    """检查 fd 和 rg 是否可用。"""
    issues = []
    for tool in ("fd", "rg"):
        found = shutil.which(tool)
        if not found:
            # 检查 vendor/bin/
            here = Path(__file__).resolve().parent.parent.parent
            vendor_bin = here / "vendor" / "bin"
            exe = vendor_bin / f"{tool}.exe" if sys.platform == "win32" else vendor_bin / tool
            if exe.is_file():
                found = str(exe)
        if not found:
            issues.append(tool)

    if not issues:
        return CheckResult("搜索工具", True, "fd + rg 可用")
    return CheckResult(
        "搜索工具", False, f"缺失: {', '.join(issues)}",
        "winget install sharkdp.fd && winget install BurntSushi.ripgrep.MSVC  或放入 vendor/bin/",
    )


def check_adb() -> CheckResult:
    """检查 adb 是否可用 + 是否有设备连接。"""
    adb = shutil.which("adb")
    if not adb:
        return CheckResult(
            "ADB 工具", False, "adb 不在 PATH",
            "安装 Android Platform Tools: https://developer.android.com/tools/releases/platform-tools",
        )
    try:
        result = subprocess.run(
            [adb, "devices"],
            capture_output=True, text=True, timeout=10,
        )
        lines = [l.strip() for l in result.stdout.strip().split("\n") if l.strip()]
        # 第一行是 "List of devices attached"
        devices = [l for l in lines[1:] if "\tdevice" in l]
        if devices:
            serials = [l.split("\t")[0] for l in devices]
            return CheckResult("ADB 设备", True, f"{len(serials)} 个设备: {', '.join(serials)}")
        return CheckResult(
            "ADB 设备", False, "adb 可用但无设备连接",
            "USB 连接 Android 设备，开启 USB 调试，运行 adb devices 确认",
        )
    except Exception as e:
        return CheckResult("ADB 设备", False, str(e), "检查 adb 安装")


# ── --fix：自动复制模板 ───────────────────────────────────────
def copy_templates() -> list[str]:
    """复制缺失的模板文件到配置目录，返回已复制的文件列表。"""
    config_dir = _config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    templates = _templates_dir()

    copied = []
    for name in ("servers.yaml", "zentao.yaml", "llm.yaml"):
        target = config_dir / name
        source = templates / f"{name}.example"
        if not target.is_file() and source.is_file():
            shutil.copy2(source, target)
            copied.append(str(target))
    return copied


# ── 主流程 ────────────────────────────────────────────────────
def run_checks() -> list[CheckResult]:
    """运行所有检查，返回结果列表。"""
    return [
        check_config_dir(),
        check_bugflow_install(),
        check_servers_yaml(),
        check_zentao_yaml(),
        check_llm_yaml(),
        check_search_tools(),
        check_adb(),
    ]


def print_results(results: list[CheckResult]) -> bool:
    """打印检查结果汇总表，返回是否全部通过。"""
    print(f"\n{'='*60}")
    print("  🔧 android-bugfix-flow 配置检查")
    print(f"{'='*60}\n")

    all_ok = True
    for r in results:
        icon = "✅" if r.ok else "❌"
        print(f"  {icon} {r.name}: {r.detail}")
        if not r.ok and r.fix_hint:
            print(f"     → 修复: {r.fix_hint}")
        if not r.ok:
            all_ok = False

    print(f"\n{'='*60}")
    if all_ok:
        print("  🎉 所有检查通过！可以开始使用了。")
        print("  提示: 运行 /bugfix-prepare 或 bugfix-prepare 开始修 bug")
    else:
        failed = sum(1 for r in results if not r.ok)
        print(f"  ⚠️ {failed} 项需要配置。按上面的修复指引操作。")
        print(f"  或运行: bugfix-setup --fix  （自动复制模板文件）")
    print(f"{'='*60}\n")

    return all_ok


def main():
    parser = argparse.ArgumentParser(
        prog="bugfix-setup",
        description="android-bugfix-flow 首次配置检查向导",
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="自动复制缺失的模板文件到 ~/.bugfix-flow/",
    )
    args = parser.parse_args()

    # --fix: 先复制模板
    if args.fix:
        print("\n📦 复制模板文件...")
        copied = copy_templates()
        if copied:
            for f in copied:
                print(f"  ✅ 已复制: {f}")
            print("  请编辑这些文件填入你的凭据。")
        else:
            print("  所有模板文件已存在，无需复制。")

    # 运行检查
    results = run_checks()
    all_ok = print_results(results)

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
