"""Launch a vendored MCP server with repo-root on PYTHONPATH.

若仓库存在 .venv，且当前解释器不是该 venv，则自动切换到 venv 再启动
（避免 Cursor 调用到系统 Python 缺依赖）。

Usage:
  python scripts/cit_mcp_launch.py zentao
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVERS = {
    "ssh": REPO / "mcp" / "ssh_server.py",
    "adb": REPO / "mcp" / "adb_server.py",
    "zentao": REPO / "mcp" / "zentao_server.py",
    "kb": REPO / "mcp" / "kb_server.py",
    "misc": REPO / "mcp" / "misc_server.py",
    "citfix": REPO / "mcp" / "citfix_server.py",
}


def _venv_python() -> Path | None:
    win = REPO / ".venv" / "Scripts" / "python.exe"
    if win.is_file():
        return win
    unix = REPO / ".venv" / "bin" / "python"
    if unix.is_file():
        return unix
    return None


def _maybe_reexec_venv() -> None:
    venv_py = _venv_python()
    if venv_py is None:
        return
    try:
        if Path(sys.executable).resolve() == venv_py.resolve():
            return
    except OSError:
        pass
    os.execv(str(venv_py), [str(venv_py), str(Path(__file__).resolve()), *sys.argv[1:]])


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in SERVERS:
        print(f"usage: {sys.argv[0]} {{{'|'.join(SERVERS)}}}", file=sys.stderr)
        return 2

    _maybe_reexec_venv()

    root = str(REPO)
    os.environ.setdefault("CIT_WORKFLOW_ROOT", root)
    os.environ.setdefault("BUGFLOW_ROOT", root)
    if root not in sys.path:
        sys.path.insert(0, root)
    os.chdir(root)
    runpy.run_path(str(SERVERS[sys.argv[1]]), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
