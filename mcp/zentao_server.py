"""zentao-mcp — 禅道查询/更新 MCP server（薄入口）。

ZCode 插件自动启动此 server（plugin.json mcpServers.zentao-mcp）。
stdio JSON-RPC → bugflow.core.zentao 转发。

工具列表：
  zentao_get_bug          — 查 bug 详情（标题/描述/附件）
  zentao_search_bugs      — 全局搜索 bug
  zentao_my_bugs          — 我的未解决 bug 列表
  zentao_download_attachment — 下载 bug 附件
  zentao_bug_commits      — 查 bug 关联的代码提交记录
  zentao_get_task         — 查任务详情
  zentao_search_tasks     — 搜索任务
  zentao_my_tasks         — 我的未完成任务列表
  zentao_task_commits     — 查任务关联的代码提交记录

配置：~/.bugfix-flow/zentao.yaml（base_url/user/password/mcp_token/mcp_secret）
"""

from __future__ import annotations

import os
import sys

# 确保 bugflow 包可 import
_here = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_here)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("ERROR: mcp SDK 未安装。请 pip install 'bugflow[mcp]'", file=sys.stderr)
    sys.exit(1)

from bugflow.core import zentao
from bugflow.core.config import _config_dir


mcp = FastMCP("zentao-mcp")


@mcp.tool()
def zentao_get_bug(bug_id: int) -> str:
    """查询禅道中指定 Bug 的详细信息。返回标题、状态、严重程度、产品、描述、附件清单。

    Args:
        bug_id: Bug ID，如 92193
    """
    try:
        return zentao.get_bug_formatted(int(bug_id))
    except zentao.ZentaoAuthError as e:
        return f"❌ 鉴权失败: {e}"
    except zentao.ZentaoError as e:
        return f"❌ 查询失败: {e}"


@mcp.tool()
def zentao_search_bugs(
    keyword: str = "",
    limit: int = 20,
    product_id: int = 0,
    status: str = "",
    page: int = 1,
    assigned_to: str = "",
) -> str:
    """在禅道中搜索 Bug（不限于自己的）。返回匹配的 Bug 列表。

    支持按关键词、产品 ID、状态、指派人任意组合过滤。至少提供一项过滤条件。
    常见用法：
      - product_id=265, status="active" → 浏览某产品所有未解决 Bug
      - keyword="GNSS" → 全局关键词搜索
      - product_id=265, keyword="WiFi" → 在某产品内按关键词搜索
      - assigned_to="liujinghua" → 查指派给某人的 Bug

    Args:
        keyword: 搜索关键词（Bug 标题或描述关键词），可空
        limit: 返回数量上限，默认 20
        product_id: 产品 ID（如 265=SRM965），0=不过滤
        status: Bug 状态过滤（active/resolved/closed），空=不过滤
        page: 页码（服务端每页最多 100 条，超出用 page=2/3... 翻页），默认 1
        assigned_to: 指派人账号（如 liujinghua），空=不过滤
    """
    try:
        return zentao.search_bugs_formatted(
            keyword,
            limit=int(limit),
            product_id=int(product_id),
            status=status,
            page=int(page),
            person=assigned_to,
        )
    except zentao.ZentaoAuthError as e:
        return f"❌ 鉴权失败: {e}"
    except zentao.ZentaoError as e:
        return f"❌ 搜索失败: {e}"


@mcp.tool()
def zentao_my_bugs(limit: int = 20) -> str:
    """查询禅道中指派给我的未解决 Bug 列表。返回 id/标题/状态/严重程度。

    Args:
        limit: 返回数量上限，默认 20
    """
    try:
        return zentao.my_bugs_formatted(limit=int(limit))
    except zentao.ZentaoAuthError as e:
        return f"❌ 鉴权失败: {e}"
    except zentao.ZentaoError as e:
        return f"❌ 查询失败: {e}"


@mcp.tool()
def zentao_list_products(limit: int = 50) -> str:
    """获取禅道产品列表（id + 名称），用于查找 product_id。

    zentao_search_bugs 的 product_id 参数需要产品 ID，用此工具查询映射。
    例如：返回 "#265  SRM965" 表示 SRM965 的 product_id=265。

    Args:
        limit: 返回数量上限，默认 50
    """
    try:
        return zentao.list_products_formatted(limit=int(limit))
    except zentao.ZentaoAuthError as e:
        return f"❌ 鉴权失败: {e}"
    except zentao.ZentaoError as e:
        return f"❌ 查询失败: {e}"


@mcp.tool()
def zentao_download_attachment(bug_id: int, file_id: int) -> str:
    """下载禅道 Bug 附件到本地 zentao_bugs/{bug_id}/ 目录。

    file_id 从 zentao_get_bug 的附件清单 [id] 获取。

    Args:
        bug_id: Bug ID
        file_id: 附件文件 ID（从 get_bug 附件清单 [id] 获取）
    """
    try:
        save_dir = str(_config_dir() / "zentao_bugs" / str(bug_id))
        return zentao.download_attachment(int(bug_id), int(file_id), save_dir)
    except zentao.ZentaoAuthError as e:
        return f"❌ 鉴权失败: {e}"
    except zentao.ZentaoError as e:
        return f"❌ 下载失败: {e}"


@mcp.tool()
def zentao_bug_commits(bug_id: int) -> str:
    """查询 Bug 关联的代码提交记录。

    解析 Bug 的 actions/comments 中的 commit hash、Gerrit Change-Id、Gerrit URL，
    并搜索版本同源 Bug（同产品+同版本前缀）的提交参考。
    降级：同产品无结果时按标题关键词搜索。

    Args:
        bug_id: Bug ID，如 92193
    """
    try:
        return zentao.query_bug_commits(int(bug_id))
    except zentao.ZentaoAuthError as e:
        return f"❌ 鉴权失败: {e}"
    except zentao.ZentaoError as e:
        return f"❌ 查询失败: {e}"


@mcp.tool()
def zentao_get_task(task_id: int) -> str:
    """获取禅道任务详情。返回标题、状态、优先级、指派、工时、描述、评论。

    Args:
        task_id: 任务 ID
    """
    try:
        return zentao.get_task_formatted(int(task_id))
    except zentao.ZentaoAuthError as e:
        return f"❌ 鉴权失败: {e}"
    except zentao.ZentaoError as e:
        return f"❌ 查询失败: {e}"


@mcp.tool()
def zentao_search_tasks(
    keyword: str = "",
    limit: int = 20,
    assigned_to: str = "",
    status: str = "",
) -> str:
    """搜索禅道任务。返回匹配的任务列表。

    支持按关键词、指派人、状态任意组合过滤。至少提供一项过滤条件。
    常见用法：
      - keyword="GNSS" → 全局关键词搜索
      - assigned_to="liujinghua" → 查指派给某人的任务
      - assigned_to="liujinghua", status="doing" → 查某人进行中的任务

    Args:
        keyword: 搜索关键词（任务标题或描述关键词），可空
        limit: 返回数量上限，默认 20
        assigned_to: 指派人账号（如 liujinghua），空=不过滤
        status: 任务状态过滤（wait/doing/pause/done/closed），空=不过滤
    """
    try:
        return zentao.search_tasks_formatted(
            keyword,
            limit=int(limit),
            person=assigned_to,
            status=status,
        )
    except zentao.ZentaoAuthError as e:
        return f"❌ 鉴权失败: {e}"
    except zentao.ZentaoError as e:
        return f"❌ 搜索失败: {e}"


@mcp.tool()
def zentao_my_tasks(limit: int = 20) -> str:
    """列出指派给我的禅道任务。返回 id/标题/状态/优先级。

    Args:
        limit: 返回数量上限，默认 20
    """
    try:
        return zentao.my_tasks_formatted(limit=int(limit))
    except zentao.ZentaoAuthError as e:
        return f"❌ 鉴权失败: {e}"
    except zentao.ZentaoError as e:
        return f"❌ 查询失败: {e}"


@mcp.tool()
def zentao_task_commits(task_id: int) -> str:
    """查询任务（需求/开发任务）关联的代码提交记录。

    解析任务的 actions/comments 中的 commit hash、Gerrit Change-Id、Gerrit URL，
    并搜索相似已完结任务的提交参考。

    Args:
        task_id: 任务 ID
    """
    try:
        return zentao.query_task_commits(int(task_id))
    except zentao.ZentaoAuthError as e:
        return f"❌ 鉴权失败: {e}"
    except zentao.ZentaoError as e:
        return f"❌ 查询失败: {e}"


@mcp.tool()
def zentao_update_bug(bug_id: int, status: str = "", comment: str = "") -> str:
    """更新禅道 Bug 状态和/或添加评论。至少传 status 或 comment 之一。

    修复流程"最后一公里"：验证通过后用此工具将 Bug 状态改为 resolved 并附验证结论评论。
    - 只传 comment：仅添加评论，不改状态
    - 传 status="resolved"：将 Bug 标记为已解决（可同时传 comment 附说明）

    Args:
        bug_id: Bug ID，如 92193
        status: 新状态（如 resolved/active），空则不改状态
        comment: 评论内容，空则不加评论
    """
    try:
        return zentao.update_bug(int(bug_id), status=status, comment=comment)
    except zentao.ZentaoAuthError as e:
        return f"❌ 鉴权失败: {e}"
    except zentao.ZentaoError as e:
        return f"❌ 更新失败: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
