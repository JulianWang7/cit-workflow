#!/usr/bin/env python3
"""渲染 .cursor/mcp.json：venv 绝对路径 + cit_mcp_launch 绝对路径。

解决 Cursor / cursor-agent 会话报「SSH MCP isn't loaded」的常见根因：
系统 Python 无 mcp SDK，或相对路径 cwd 不对。

用法（仓库根）:
  .\\.venv\\Scripts\\python.exe scripts\\cit_render_mcp_json.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / ".cursor" / "mcp.json"
LAUNCH = REPO / "scripts" / "cit_mcp_launch.py"
SERVERS = ("ssh", "adb", "zentao", "kb", "misc", "citfix")


def _prefer_venv_python() -> str:
    override = os.environ.get("CIT_BUGFLOW_PYTHON")
    if override:
        return override
    win = REPO / ".venv" / "Scripts" / "python.exe"
    if win.is_file():
        return str(win.resolve())
    unix = REPO / ".venv" / "bin" / "python"
    if unix.is_file():
        return str(unix.resolve())
    return str(Path(sys.executable).resolve())


def main() -> int:
    if not LAUNCH.is_file():
        print(f"缺少 {LAUNCH}", file=sys.stderr)
        return 1

    cfg_dir = os.environ.get("BUGFIX_CONFIG_DIR") or str(Path.home() / ".bugfix-flow")
    py = _prefer_venv_python()
    launch = str(LAUNCH.resolve())

    servers = {}
    for name in SERVERS:
        servers[f"{name}-mcp"] = {
            "type": "stdio",
            "command": py,
            "args": [launch, name],
            "env": {
                "BUGFIX_CONFIG_DIR": cfg_dir,
                "CIT_WORKFLOW_ROOT": str(REPO),
                "BUGFLOW_ROOT": str(REPO),
                "PYTHONPATH": str(REPO),
            },
        }

    OUT.write_text(
        json.dumps({"mcpServers": servers}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"已写入 {OUT}")
    print(f"python={py}")
    print(f"repo={REPO}")
    print("下一步: 完全退出 Cursor / 新开 cursor-agent 会话，再 /citfix。")
    print("共享仓提交前如需相对路径模板，可从 git 还原 .cursor/mcp.json。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
