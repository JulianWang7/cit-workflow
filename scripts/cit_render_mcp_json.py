#!/usr/bin/env python3
"""渲染 .cursor/mcp.json：把 scripts/cit_mcp_launch.py 写成绝对路径。

MCP 实现已内嵌于本仓库 mcp/ + bugflow/。本脚本仅解决 Cursor 以非仓库
cwd 启动、或相对路径失效时的本机注册问题。

用法（在仓库根）:
  set BUGFIX_CONFIG_DIR=%USERPROFILE%\\.bugfix-flow
  python scripts/cit_render_mcp_json.py
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


def main() -> int:
    if not LAUNCH.is_file():
        print(f"缺少 {LAUNCH}", file=sys.stderr)
        return 1

    cfg_dir = os.environ.get("BUGFIX_CONFIG_DIR") or str(Path.home() / ".bugfix-flow")
    py = os.environ.get("CIT_BUGFLOW_PYTHON") or sys.executable

    servers = {}
    for name in SERVERS:
        servers[f"{name}-mcp"] = {
            "type": "stdio",
            "command": py,
            "args": [str(LAUNCH), name],
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
    print("提示: 绝对路径版适合本机；分享远程仓库请保留相对路径版或重新生成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
