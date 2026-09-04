#!/usr/bin/env python3
"""Smoke-check cit-workflow MCP launch path (ssh/zentao/…), no long-lived server.

Usage (repo root):
  .\\.venv\\Scripts\\python.exe scripts\\cit_smoke_mcp.py
  .\\.venv\\Scripts\\python.exe scripts\\cit_smoke_mcp.py --render
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LAUNCH = REPO / "scripts" / "cit_mcp_launch.py"
MCP_JSON = REPO / ".cursor" / "mcp.json"
SERVERS = ("ssh", "adb", "zentao", "kb", "misc", "citfix")


def _venv_python() -> Path:
    win = REPO / ".venv" / "Scripts" / "python.exe"
    if win.is_file():
        return win
    unix = REPO / ".venv" / "bin" / "python"
    if unix.is_file():
        return unix
    return Path(sys.executable)


def _ok(msg: str) -> None:
    print(f"  OK  {msg}")


def _bad(msg: str) -> None:
    print(f"  FAIL {msg}")


def _warn(msg: str) -> None:
    print(f"  WARN {msg}")


def check_imports(py: Path) -> bool:
    print("== imports ==")
    r = subprocess.run(
        [str(py), "-c", "import mcp, bugflow, yaml; print(mcp.__file__); print(bugflow.__file__)"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        _bad(f"import failed: {(r.stderr or r.stdout)[:500]}")
        _warn("Fix: .\\.venv\\Scripts\\pip.exe install -r requirements-mcp.txt")
        return False
    _ok((r.stdout or "").strip().replace("\n", " | "))
    return True


def check_mcp_json() -> bool:
    print("== .cursor/mcp.json ==")
    if not MCP_JSON.is_file():
        _bad(f"missing {MCP_JSON}")
        return False
    import json

    raw = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    servers = raw.get("mcpServers") or {}
    missing = [f"{n}-mcp" for n in SERVERS if f"{n}-mcp" not in servers]
    if missing:
        _bad(f"missing servers: {missing}")
        return False
    ssh = servers.get("ssh-mcp") or {}
    cmd = ssh.get("command")
    args = ssh.get("args") or []
    _ok(f"ssh-mcp command={cmd!r} args0={args[:1]!r}")
    # Relative launch is OK if cwd=repo; warn if command is bare python
    if str(cmd) in ("python", "python3") and not any("cit_mcp_launch" in str(a) for a in args):
        _warn("ssh-mcp args look wrong — expect scripts/cit_mcp_launch.py ssh")
    if str(cmd) in ("python", "python3"):
        _warn(
            "command is bare 'python' — Cursor may use system Python without mcp SDK. "
            "Prefer: python scripts/cit_render_mcp_json.py  (writes venv absolute paths)"
        )
    return True


def check_config_dir() -> bool:
    print("== BUGFIX_CONFIG_DIR ==")
    cfg = os.environ.get("BUGFIX_CONFIG_DIR") or str(Path.home() / ".bugfix-flow")
    p = Path(cfg)
    if not p.is_dir():
        _bad(f"config dir missing: {p}")
        return False
    _ok(str(p))
    for name in ("zentao.yaml", "servers.yaml"):
        f = p / name
        if f.is_file():
            _ok(f"found {name}")
        else:
            _warn(f"missing {name} — copy from config/citfix/*.example")
    return True


def check_launch_help(py: Path) -> bool:
    print("== cit_mcp_launch ==")
    r = subprocess.run(
        [str(py), str(LAUNCH)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
    )
    # usage exit 2 is expected
    err = (r.stderr or r.stdout or "")
    if "cit_mcp_launch.py" in err or "usage:" in err:
        _ok("launcher reachable")
        return True
    _bad(err[:400] or f"exit={r.returncode}")
    return False


def check_ssh_module(py: Path) -> bool:
    print("== ssh_server import ==")
    code = (
        "import runpy, sys; sys.path.insert(0, r'%s'); "
        "import importlib.util; "
        "spec=importlib.util.spec_from_file_location('ssh_server', r'%s'); "
        "m=importlib.util.module_from_spec(spec); "
        # Don't run FastMCP main — just compile/load top until mcp init
        "print('skip_full_load')"
        % (REPO.as_posix(), (REPO / "mcp" / "ssh_server.py").as_posix())
    )
    # Simpler: just ensure file exists and bugflow.ssh importable
    r = subprocess.run(
        [
            str(py),
            "-c",
            "from bugflow.core import ssh, config; "
            "print('ssh_ok', hasattr(ssh, 'ssh_exec')); "
            "st=config.get_config_status(); "
            "print('zentao_ready', (st.get('zentao') or {}).get('ready')); "
            "print('servers', list((st.get('servers') or {}).get('names') or (st.get('servers') or {}).keys())[:5] "
            "if isinstance(st.get('servers'), dict) else st.get('servers'))",
        ],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(REPO)},
    )
    if r.returncode != 0:
        _bad((r.stderr or r.stdout or "")[:500])
        return False
    _ok((r.stdout or "").strip().replace("\n", " | "))
    return True


def print_remediation() -> None:
    print()
    print("== If Cursor says: SSH MCP isn't loaded in this session ==")
    print("  1. Workspace MUST be cit-workflow repo root (folder that contains .cursor/mcp.json)")
    print("  2. Render absolute MCP launchers:")
    print("       .\\.venv\\Scripts\\python.exe scripts\\cit_render_mcp_json.py")
    print("  3. Fully quit & reopen Cursor / start a NEW cursor-agent session")
    print("  4. In IDE: Settings → MCP → confirm ssh-mcp is green/enabled")
    print("  5. Re-run:  cursor-agent --workspace D:\\Workspace\\cit-workflow \"/citfix <id> --resume\"")
    print("  6. Agent MUST call MCP tool set_workspace / search_code_tool — not silent bugflow import")
    print("  Approved non-MCP path for commit only: scripts/cit_closure_run.py (Worker)")


def main() -> int:
    ap = argparse.ArgumentParser(description="cit-workflow MCP smoke")
    ap.add_argument("--render", action="store_true", help="run cit_render_mcp_json.py first")
    args = ap.parse_args()
    py = _venv_python()
    print(f"repo={REPO}")
    print(f"python={py}")

    if args.render:
        print("== render mcp.json ==")
        r = subprocess.run([str(py), str(REPO / "scripts" / "cit_render_mcp_json.py")], cwd=str(REPO))
        if r.returncode != 0:
            return r.returncode

    ok = True
    ok = check_imports(py) and ok
    ok = check_mcp_json() and ok
    ok = check_config_dir() and ok
    ok = check_launch_help(py) and ok
    ok = check_ssh_module(py) and ok
    print_remediation()
    print()
    print("PASS" if ok else "FAIL — fix items above before /citfix agent stages that need SSH")
    return 0 if ok else 1


if __name__ == "__main__":
    # path shadowing guard when run as script
    _scripts = str(Path(__file__).resolve().parent)
    while sys.path and sys.path[0] in ("", ".", _scripts):
        sys.path.pop(0)
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    raise SystemExit(main())
