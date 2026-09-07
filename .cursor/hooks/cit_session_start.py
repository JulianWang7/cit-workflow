"""SessionStart hook — CIT 环境准备自检（不阻断会话）。

检查：
1. 仓内 mcp/ + bugflow/ 是否齐全
2. BUGFIX_CONFIG_DIR（或默认 ~/.bugfix-flow）
3. 常用第三方包（缺失则尝试 pip install）
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

REQUIRED_PACKAGES: dict[str, str] = {
    "paramiko": "paramiko",
    "yaml": "pyyaml",
    "openpyxl": "openpyxl",
    "cryptography": "cryptography",
    "serial": "pyserial",
}

MCP_ENTRYPOINTS = (
    "mcp/ssh_server.py",
    "mcp/adb_server.py",
    "mcp/zentao_server.py",
    "mcp/kb_server.py",
    "mcp/misc_server.py",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _warn(msg: str) -> None:
    print(f"[cit-workflow] {msg}", file=sys.stderr)


def _check_tree(root: Path) -> None:
    if not (root / "bugflow").is_dir():
        _warn(f"缺少 bugflow 包目录: {root / 'bugflow'}")
    missing = [rel for rel in MCP_ENTRYPOINTS if not (root / rel).is_file()]
    if missing:
        _warn(f"缺少 MCP 入口: {', '.join(missing)}")
    else:
        _warn(f"仓内 MCP OK: {root}")

    cfg = os.environ.get("BUGFIX_CONFIG_DIR", "").strip()
    if not cfg:
        home_cfg = Path.home() / ".bugfix-flow"
        _warn(f"未设置 BUGFIX_CONFIG_DIR（默认 {home_cfg}）")
    elif not Path(cfg).is_dir():
        _warn(f"BUGFIX_CONFIG_DIR 不存在: {cfg}（请先 cit-setup）")


def _check_missing_packages() -> list[str]:
    missing: list[str] = []
    for mod_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(mod_name)
        except Exception:
            missing.append(pip_name)
    try:
        importlib.import_module("mcp")
    except Exception:
        missing.append("mcp")
    try:
        importlib.import_module("bugflow")
    except Exception:
        _warn("bugflow 未能 import（确认仓库根在 PYTHONPATH 或已 pip install -e .）")
    return missing


def _install_packages(pip_names: list[str]) -> None:
    if not pip_names:
        return
    _warn(f"安装缺失依赖: {' '.join(pip_names)}")
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *pip_names, "-q"],
            timeout=180,
        )
    except Exception as e:
        _warn(f"依赖安装失败（不阻断）: {e}")


def main() -> None:
    try:
        root = _repo_root()
        os.chdir(root)
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        os.environ.setdefault("CIT_WORKFLOW_ROOT", str(root))
        os.environ.setdefault("BUGFLOW_ROOT", str(root))
        _warn(
            "维护约束：业务框架=EXP-CIT-004§2-5；工具=项目 MCP/Skill；"
            "runs_work 可读，持久写入须事先告知"
        )
        _check_tree(root)
        missing = _check_missing_packages()
        if missing:
            _install_packages(missing)
    except Exception as e:
        _warn(f"SessionStart hook 异常（不阻断）: {e}")
    sys.exit(0)


if __name__ == "__main__":
    main()
