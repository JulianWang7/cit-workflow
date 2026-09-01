"""共享能力层：SSH/ADB/搜索/编译/git/config/zentao。

MCP server（repo 根 mcp/）和 orchestrator（bugflow.cli）都 import 这一层，
逻辑单一源不重复。所有实现自包含，不依赖 YWAgent。
"""

from bugflow.core.ssh import ssh_exec, get_connection, ssh_disconnect_all
from bugflow.core.adb import adb_run, resolve_device, wait_for_device, wait_for_boot_completed
from bugflow.core.search import search_code, locate_files, bundled_binary
from bugflow.core.compile import build_module
from bugflow.core.git import git_exec
from bugflow.core.config import load_server_config, get_bugfix_env, load_zentao_config, load_llm_config
from bugflow.core.zentao import (
    get_bug as zentao_get_bug,
    get_bug_formatted as zentao_get_bug_formatted,
    search_bugs as zentao_search_bugs,
    my_bugs as zentao_my_bugs,
    update_bug as zentao_update_bug,
    download_attachment as zentao_download_attachment,
    fetch_bug_as_bugitem as zentao_fetch_bug_as_bugitem,
    list_products_formatted as zentao_list_products_formatted,
    ZentaoError,
    ZentaoAuthError,
)

__all__ = [
    "ssh_exec", "get_connection", "ssh_disconnect_all",
    "adb_run", "resolve_device", "wait_for_device", "wait_for_boot_completed",
    "search_code", "locate_files", "bundled_binary",
    "build_module", "git_exec",
    "load_server_config", "get_bugfix_env", "load_zentao_config", "load_llm_config",
    "zentao_get_bug", "zentao_get_bug_formatted", "zentao_search_bugs",
    "zentao_my_bugs", "zentao_update_bug", "zentao_download_attachment",
    "zentao_fetch_bug_as_bugitem", "zentao_list_products_formatted",
    "ZentaoError", "ZentaoAuthError",
]
