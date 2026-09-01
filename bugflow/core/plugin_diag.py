# -*- coding: utf-8 -*-
"""插件自诊断 — 自动检查常见问题。

检查项：
1. 配置文件：YAML 字段名是否正确（user 不是 username）
2. Core 统一入口：每个 tools 层调用的 core 函数是否存在
3. 知识库文件：kb.sqlite / kb_tasks.sqlite / kb_wiki.sqlite 是否存在
4. plugin.json 与 plugin.py 工具数一致性
5. Core 模块可导入性
6. 配置完整性：各服务是否 ready

纯本地检查，不需要 SSH 或网络。
"""

from __future__ import annotations

import ast
import importlib
import json
import sys
from pathlib import Path

from .config import (
    _config_dir,
    get_config_status,
    load_gerrit_config,
    load_daily_report_config,
    load_wiki_config,
    load_zentao_config,
)


def _plugin_dir() -> Path:
    """插件根目录（本文件在 bugflow/core/ 下，上三级就是根）。"""
    return Path(__file__).resolve().parent.parent.parent


def _check_config_fields() -> list[dict]:
    """检查 YAML 配置文件字段名是否正确。"""
    results = []
    config_dir = _config_dir()

    checks = [
        ("zentao.yaml", "zentao", "user"),
        ("gerrit.yaml", "gerrit", "user"),
        ("wiki.yaml", "wiki", "user"),
        ("daily_report.yaml", "daily_report", "user"),
    ]

    import yaml
    for filename, top_key, expected_user_field in checks:
        path = config_dir / filename
        item = {"name": f"配置文件 {filename}", "ok": True, "detail": ""}
        if not path.is_file():
            item["ok"] = False
            item["detail"] = f"文件不存在（{path}）"
            item["fix"] = f"调 config(action='save', config_type='{top_key}', data={{...}}) 创建"
            results.append(item)
            continue

        try:
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cfg = data.get(top_key, {}) if isinstance(data, dict) else {}

            # 检查 username（应为 user）
            if "username" in cfg and "user" not in cfg:
                item["ok"] = False
                item["detail"] = f"字段名是 'username'，应为 'user'"
                item["fix"] = f"改 {filename} 中 username → user，或重新调 config(action='save')"
            elif "username" in cfg and "user" in cfg:
                item["ok"] = False
                item["detail"] = f"同时有 'username' 和 'user'，应删掉 'username'"
                item["fix"] = f"删 {filename} 中的 username 行"
            else:
                item["detail"] = f"字段名正确（{', '.join(cfg.keys())}）"

            # 检查必填字段
            if top_key == "zentao":
                missing = [f for f in ("user", "password", "mcp_token", "mcp_secret") if not cfg.get(f)]
            else:
                missing = [f for f in ("user", "password") if not cfg.get(f)]
            if missing:
                item["ok"] = False
                item["detail"] += f"；缺字段: {', '.join(missing)}"
                item["fix"] = f"补上缺失的账号密码"
        except Exception as e:
            item["ok"] = False
            item["detail"] = f"读取失败: {e}"

        results.append(item)

    return results


def _check_core_entry_points() -> list[dict]:
    """检查 tools 层调用的 core 统一入口函数是否存在。

    解析 tools/*_tools.py 中的 `from core import xxx as _xxx` 和 `_xxx.func(` 调用，
    再检查 core/xxx.py 中是否定义了 func。
    """
    results = []
    plugin_dir = _plugin_dir()

    # 已知的 tools → core 调用映射（手动维护，覆盖统一入口模式的关键路径）
    # 只检查走 _module.func(action=...) 统一入口模式的工具
    known_checks = [
        # (tools_file, import_statement, core_module, called_func)
        ("wiki_tools.py", "wiki", "wiki.py", "wiki"),
        ("kb_tools.py", "kb_wiki", "kb_wiki.py", "search_kb_wiki_formatted"),
        ("kb_tools.py", "kb_wiki", "kb_wiki.py", "kb_wiki_stats"),
        ("config_tools.py", "config", "config.py", "save_config"),
        ("config_tools.py", "config", "config.py", "get_config_status"),
        ("gerrit_tools.py", "gerrit", "gerrit.py", "gerrit"),
        ("daily_report_tools.py", "daily_report", "daily_report.py", "daily_report"),
        ("zentao_tools.py", "zentao", "zentao.py", "get_bug_formatted"),
    ]

    for tools_file, core_mod_alias, core_file, func_name in known_checks:
        core_path = plugin_dir / "bugflow" / "core" / core_file
        item = {
            "name": f"core/{core_file} → {func_name}()",
            "ok": True,
            "detail": "",
        }
        if not core_path.is_file():
            item["ok"] = False
            item["detail"] = f"core 文件不存在: {core_path}"
            results.append(item)
            continue

        try:
            tree = ast.parse(core_path.read_text(encoding="utf-8"))
            defined = set()
            for n in ast.walk(tree):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defined.add(n.name)
            if func_name in defined:
                item["detail"] = "✅ 存在"
            else:
                item["ok"] = False
                item["detail"] = f"❌ core/{core_file} 中没有 '{func_name}' 函数"
                item["fix"] = (
                    f"在 core/{core_file} 中添加 def {func_name}(action=...) 统一入口，"
                    f"分发到已有的独立函数。参考 wiki.py 的 wiki(action=...) 实现。"
                )
        except Exception as e:
            item["ok"] = False
            item["detail"] = f"解析失败: {e}"

        results.append(item)

    return results


def _check_kb_files() -> list[dict]:
    """检查知识库 SQLite 文件是否存在。"""
    results = []
    config_dir = _config_dir()

    kb_files = [
        ("kb.sqlite", "Bug 知识库"),
        ("kb_docs.sqlite", "文档知识库"),
        ("kb_aosp.sqlite", "AOSP 知识库"),
        ("kb_wiki.sqlite", "Wiki 知识库"),
        ("kb_tasks.sqlite", "Task 知识库"),
        ("kb_cases.sqlite", "案例知识库"),
    ]

    for filename, label in kb_files:
        path = config_dir / filename
        item = {"name": f"{label} ({filename})", "ok": True, "detail": ""}
        if path.is_file():
            size = path.stat().st_size
            item["detail"] = f"存在 ({size:,} bytes)"
            if size < 100:
                item["ok"] = False
                item["detail"] += " ⚠️ 文件过小，可能为空"
                item["fix"] = f"调用 kb_sync(kb_type=\"all\") 从 85 共享同步"
        else:
            item["ok"] = False
            item["detail"] = "不存在"
            item["fix"] = "调用 kb_sync(kb_type=\"all\") 从 85 共享同步，或运行对应 build_kb_*.py 脚本构建"
        results.append(item)

    return results


def _check_plugin_json_consistency() -> list[dict]:
    """检查 plugin.json 与 plugin.py 的工具数一致性。"""
    results = []
    plugin_dir = _plugin_dir()
    json_path = plugin_dir / ".zcode-plugin" / "plugin.json"
    py_path = plugin_dir / "plugin.py"

    item = {"name": "plugin.json ↔ plugin.py 工具数一致性", "ok": True, "detail": ""}

    # plugin.json 工具数
    try:
        with json_path.open("r", encoding="utf-8") as f:
            meta = json.load(f)
        json_tools = {t["name"] for t in meta.get("meta", {}).get("tools", [])}
    except Exception as e:
        item["ok"] = False
        item["detail"] = f"plugin.json 解析失败: {e}"
        results.append(item)
        return results

    # plugin.py _TOOLS 列表
    if not py_path.is_file():
        # 本插件用 MCP server 薄入口，无 plugin.py
        item["detail"] = f"plugin.json 工具 {len(json_tools)} 个；无 plugin.py（MCP 薄入口架构）"
        results.append(item)
        return results

    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        py_tools = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "_TOOLS":
                        if isinstance(node.value, ast.List):
                            for elt in node.value.elts:
                                if isinstance(elt, ast.Tuple) and len(elt.elts) >= 1:
                                    if isinstance(elt.elts[0], ast.Constant):
                                        py_tools.add(elt.elts[0].value)
    except Exception as e:
        item["ok"] = False
        item["detail"] = f"plugin.py 解析失败: {e}"
        results.append(item)
        return results

    if json_tools == py_tools:
        item["detail"] = f"一致（{len(py_tools)} 个工具）"
    else:
        only_json = json_tools - py_tools
        only_py = py_tools - json_tools
        item["ok"] = False
        parts = []
        if only_json:
            parts.append(f"plugin.json 多: {only_json}")
        if only_py:
            parts.append(f"plugin.py 多: {only_py}")
        item["detail"] = "；".join(parts)
        item["fix"] = "对齐 plugin.json meta.tools 和 plugin.py _TOOLS 列表"

    results.append(item)
    return results


def _check_imports() -> list[dict]:
    """检查所有 core 模块可导入。"""
    results = []
    plugin_dir = _plugin_dir()

    # 确保 plugin_dir 在 sys.path
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))

    core_modules = [
        "bugflow.core.config",
        "bugflow.core.wiki",
        "bugflow.core.kb_wiki",
        "bugflow.core.gerrit",
        "bugflow.core.daily_report",
        "bugflow.core.zentao",
        "bugflow.core.adb",
        "bugflow.core.ssh",
        "bugflow.core.compile",
        "bugflow.core.git",
        "bugflow.core.http_client",
        "bugflow.core.excel_export",
        "bugflow.core.plugin_diag",
    ]

    for mod_name in core_modules:
        item = {"name": f"import {mod_name}", "ok": True, "detail": ""}
        try:
            importlib.import_module(mod_name)
            item["detail"] = "✅"
        except ImportError as e:
            item["ok"] = False
            item["detail"] = f"ImportError: {e}"
            item["fix"] = f"检查 {mod_name} 的依赖是否安装（requirements.txt）"
        except Exception as e:
            item["ok"] = False
            item["detail"] = f"{type(e).__name__}: {e}"
        results.append(item)

    return results


def _check_config_status() -> list[dict]:
    """检查各服务配置是否 ready。"""
    results = []
    try:
        status = get_config_status()
    except Exception as e:
        return [{"name": "配置状态检查", "ok": False, "detail": f"get_config_status() 失败: {e}"}]

    service_labels = {
        "zentao": "禅道",
        "gerrit": "Gerrit",
        "wiki": "Wiki",
        "daily_report": "日报站",
    }
    for key, label in service_labels.items():
        s = status.get(key, {})
        item = {"name": f"{label} 配置", "ok": s.get("ready", False), "detail": ""}
        missing = []
        if not s.get("has_user"):
            missing.append("用户名")
        if not s.get("has_password"):
            missing.append("密码")
        if key == "zentao" and not s.get("has_mcp"):
            missing.append("MCP Token/Secret")
        if missing:
            item["detail"] = f"缺: {', '.join(missing)}"
            item["fix"] = f"调 config(action='save', config_type='{key}', data={{...}})"
        else:
            item["detail"] = "✅ ready"

        results.append(item)

    # Workspace
    ws = status.get("workspace", {})
    item = {"name": "工作区", "ok": ws.get("set", False), "detail": ""}
    if ws.get("set"):
        item["detail"] = f"✅ {ws.get('server')} → {ws.get('code_root')}"
    else:
        item["detail"] = "未设置"
        item["fix"] = "调 set_workspace(server='252', code_root='/home7/.../SRM965')"
    results.append(item)

    return results


def diagnose() -> str:
    """运行全部诊断检查，返回格式化报告。"""
    sections: list[tuple[str, list[dict]]] = [
        ("📋 配置完整性", _check_config_status()),
        ("📄 配置文件字段名", _check_config_fields()),
        ("🔗 Core 统一入口函数", _check_core_entry_points()),
        ("📦 知识库文件", _check_kb_files()),
        ("🧩 plugin.json 一致性", _check_plugin_json_consistency()),
        ("🐍 模块导入", _check_imports()),
    ]

    lines = ["# 🔧 插件自诊断报告\n"]
    total_ok = 0
    total_fail = 0

    for title, items in sections:
        section_fail = sum(1 for i in items if not i["ok"])
        section_ok = len(items) - section_fail
        total_ok += section_ok
        total_fail += section_fail

        status_icon = "✅" if section_fail == 0 else f"⚠️ {section_fail} 项失败"
        lines.append(f"\n## {title} ({status_icon})\n")

        for item in items:
            icon = "✅" if item["ok"] else "❌"
            lines.append(f"- {icon} **{item['name']}**: {item['detail']}")
            if not item["ok"] and item.get("fix"):
                lines.append(f"  - 🔧 修复: {item['fix']}")

    lines.append(f"\n---\n**总计**: {total_ok} 通过, {total_fail} 失败")

    if total_fail == 0:
        lines.append("\n\n🎉 插件状态良好，未发现问题。")
    else:
        lines.append(f"\n\n⚠️ 发现 {total_fail} 个问题，按上述 🔧 修复建议处理。")
        lines.append("加载 `plugin_maintenance` skill 获取详细诊断流程。")

    return "\n".join(lines)


def diagnose_summary() -> dict:
    """返回诊断摘要（供程序调用）。"""
    sections = {
        "config_status": _check_config_status(),
        "config_fields": _check_config_fields(),
        "core_entry_points": _check_core_entry_points(),
        "kb_files": _check_kb_files(),
        "plugin_json": _check_plugin_json_consistency(),
        "imports": _check_imports(),
    }
    total = sum(len(v) for v in sections.values())
    failed = sum(sum(1 for i in v if not i["ok"]) for v in sections.values())
    return {
        "total_checks": total,
        "failed": failed,
        "passed": total - failed,
        "healthy": failed == 0,
        "sections": sections,
    }
