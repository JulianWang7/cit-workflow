"""kb-mcp — 知识库 / Gerrit / Wiki / 视觉识图统一 MCP server（薄入口）。

ZCode 插件自动启动此 server（plugin.json mcpServers.kb-mcp）。
stdio JSON-RPC → bugflow.core 转发。

工具列表：
  kb_search          — 搜索知识库 (bug/aosp/task/wiki/cases/docs)
  kb_stats           — 知识库统计
  kb_reindex         — 重建向量索引
  kb_sync            — 从 85 共享同步知识库 SQLite 到本地
  gerrit_search      — Gerrit 代码审查统一入口（搜索/详情/diff/cat/review...）
  wiki_search        — 内部 Wiki 搜索
  vision_describe    — 视觉识图（截图分析）

配置：
  ~/.bugfix-flow/gerrit.yaml（base_url/user/password）
  ~/.bugfix-flow/wiki.yaml（base_url/user/password）
  ~/.bugfix-flow/llm.yaml（model/base_url/api_key）— 视觉用
  ~/.bugfix-flow/providers/custom/openai-custom-new.json — 视觉 API 凭证
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime

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

from bugflow.core import kb, kb_aosp, kb_cases, kb_docs, kb_task, kb_wiki
from bugflow.core import kb_sync as _kb_sync
from bugflow.core import gerrit as gerrit_mod
from bugflow.core import vision, wiki as wiki_mod
from bugflow.core import batch_state

logger = logging.getLogger("bugflow.mcp.kb")

mcp = FastMCP("kb-mcp")

# ── kb_type → 模块路由表 ──────────────────────────────────────────
_KB_DISPATCH = {
    "bug": lambda kw, lim, dom: kb.search_kb_formatted(keyword=kw, limit=lim),
    "aosp": lambda kw, lim, dom: kb_aosp.search_kb_aosp_formatted(keyword=kw, limit=lim),
    "task": lambda kw, lim, dom: kb_task.search_kb_task_formatted(keyword=kw, limit=lim),
    "wiki": lambda kw, lim, dom: kb_wiki.search_kb_wiki_formatted(keyword=kw, limit=lim),
    "cases": lambda kw, lim, dom: kb_cases.search_kb_cases_formatted(keyword=kw, limit=lim),
    "docs": lambda kw, lim, dom: kb_docs.search_kb_docs_formatted(
        keyword=kw, tier=dom, limit=lim
    ),
}

# ── kb_type → 统计路由表 ──────────────────────────────────────────
_KB_STATS_DISPATCH = {
    "bug": lambda: kb.kb_stats(),
    "aosp": lambda: kb_aosp.kb_aosp_stats(),
    "task": lambda: kb_task.kb_task_stats(),
    "wiki": lambda: kb_wiki.kb_wiki_stats(),
    "cases": lambda: kb_cases.kb_cases_stats(),
    "docs": lambda: kb_docs.kb_docs_stats(),
}

# ── kb_type → 重建索引路由表 ──────────────────────────────────────
# wiki 知识库为纯 SQL LIKE（无向量索引），不参与 reindex。
_KB_REINDEX_DISPATCH = {
    "bug": lambda model: kb.reindex_kb(model),
    "aosp": lambda model: kb_aosp.reindex_kb_aosp(model),
    "task": lambda model: kb_task.reindex_kb_task(model),
    "cases": lambda model: kb_cases.reindex_kb_cases(model),
    "docs": lambda model: kb_docs.reindex_kb_docs(model),
}


# ── 工具 1: kb_search ─────────────────────────────────────────────

@mcp.tool()
def kb_search(keyword: str, kb_type: str = "bug", limit: int = 10, domain: str = "") -> str:
    """搜索知识库。支持 6 种知识库类型，返回格式化文本。

    Args:
        keyword: 搜索关键词（如 "ANR WindowManager" / "bootloop init"）
        kb_type: 知识库类型 — bug(默认)/aosp/task/wiki/cases/docs
            - bug:   历史 Bug 知识库（禅道导出，含解决方案）
            - aosp:  AOSP 架构追踪/案例复盘/实现配方
            - task:  任务知识库
            - wiki:  内部 Wiki 页面索引
            - cases: 已审核修复案例（修复成功后自动积累）
            - docs:  文档知识库（模块手册/平台指南/技术期刊，24K+ 文档）
        limit: 返回数量上限，默认 10
        domain: 领域过滤（可选）。仅 docs 消费此参数，映射为 tier 层级过滤：
            P0（自有知识）/ P1（平台参考）/ P2（期刊项目）。其余 kb_type 忽略。
    """
    kb_type_norm = (kb_type or "bug").strip().lower()
    handler = _KB_DISPATCH.get(kb_type_norm)
    if handler is None:
        valid = "/".join(_KB_DISPATCH.keys())
        return f"❌ 未知 kb_type「{kb_type}」。支持: {valid}"
    try:
        return handler(keyword or "", int(limit), domain or "")
    except Exception as e:
        logger.exception("kb_search 异常 (type=%s)", kb_type_norm)
        return f"❌ 知识库搜索失败: {e}"


# ── 工具 2: gerrit_search ─────────────────────────────────────────

@mcp.tool()
def gerrit_search(
    query: str = "",
    action: str = "search",
    change_id: str = "",
    status: str = "open",
    limit: int = 10,
    repo: str = "",
    branch: str = "",
    path: str = "",
    max_chars: int = 20000,
    lines: str = "",
    message: str = "",
    score: int = 0,
    save_to: str = "",
    topic: str = "",
    reviewer: str = "",
    state: str = "REVIEWER",
    line: int = 0,
    context: int = 3,
) -> str:
    """Gerrit 代码审查统一入口。支持搜索/详情/diff/cat/review 等操作。

    Args:
        query: 搜索查询（action=search 时必填，如 "status:open project:platform/frameworks/base"）
        action: 操作类型 —
            search(默认): 搜 Change
            get_change: Change 详情
            list_files: 变更文件列表
            list_comments: 行级评论列表
            commit_message: commit message
            diff: patch / 单文件 diff
            download_patch: 下载 patch 到本地
            my_changes: 当前用户提交
            review: 发表评论/打分
            review_line: 行级评论
            add_reviewer: 添加 reviewer/CC
            abandon: Abandon Change
            set_topic: 设置/清除 topic
            list_projects / list_branches / ls / cat: Gitiles 远程读码
        change_id: Change ID（get_change/diff/review 等需要）
        status: 状态过滤（my_changes 用），默认 open
        limit: 返回上限，默认 10
        repo: 仓库名（cat/ls 用，如 "platform/frameworks/base"）
        branch: 分支名（cat/ls 用，如 "main"）
        path: 文件路径（cat/diff/review_line 用）
        max_chars: cat/diff 最大字符数，默认 20000
        lines: cat 行范围（如 "10-50"）
        message: review/review_line 评论内容
        score: review 打分（-2~-1 / 0 / +1~+2）
        save_to: download_patch 保存路径
        topic: set_topic 的 topic 名
        reviewer: add_reviewer 的 reviewer 账号
        state: add_reviewer 状态（REVIEWER/CC），默认 REVIEWER
        line: review_line 行号
        context: diff 上下文行数，默认 3
    """
    try:
        return gerrit_mod.gerrit(
            action=action,
            query=query,
            change_id=change_id,
            status=status,
            limit=int(limit),
            repo=repo,
            branch=branch,
            path=path,
            max_chars=int(max_chars),
            lines=lines,
            message=message,
            score=int(score),
            save_to=save_to,
            topic=topic,
            reviewer=reviewer,
            state=state,
            line=int(line),
            context=int(context),
        )
    except gerrit_mod.GerritError as e:
        return f"❌ Gerrit 操作失败: {e}"
    except Exception as e:
        logger.exception("gerrit_search 异常 (action=%s)", action)
        return f"❌ Gerrit 操作失败: {e}"


# ── 工具 3: wiki_search ───────────────────────────────────────────

@mcp.tool()
def wiki_search(keyword: str, limit: int = 10) -> str:
    """搜索内部 Wiki 页面。返回匹配的页面标题和摘要。

    Args:
        keyword: 搜索关键词（如 "代码审查规范" / "提交规则"）
        limit: 返回数量上限，默认 10
    """
    try:
        return wiki_mod.wiki_search(keyword or "", limit=int(limit))
    except wiki_mod.WikiError as e:
        return f"❌ Wiki 搜索失败: {e}"
    except Exception as e:
        logger.exception("wiki_search 异常")
        return f"❌ Wiki 搜索失败: {e}"


# ── 工具 4: vision_describe ───────────────────────────────────────

@mcp.tool()
def vision_describe(image_path: str, prompt: str = "") -> str:
    """调视觉模型识图，返回文本描述。用于分析截图/日志图片中的异常现象。

    Args:
        image_path: 本地图像文件路径（PNG/JPG）。也可传 base64 字符串或 data URL。
        prompt: 要视觉模型回答的问题。空则用默认描述任务
                （"描述异常现象和可见 UI 元素"）。
    """
    try:
        return vision.describe_image(image=image_path, question=prompt or "")
    except FileNotFoundError as e:
        return f"❌ 图像文件不存在: {e}"
    except ValueError as e:
        return f"❌ 图像文件无效: {e}"
    except Exception as e:
        logger.exception("vision_describe 异常")
        return f"❌ 视觉识别失败: {e}"


# ── 工具 5: kb_stats ──────────────────────────────────────────────

@mcp.tool()
def kb_stats(kb_type: str = "bug") -> str:
    """查看知识库统计信息（条目数、最后更新时间等）。
    kb_type: bug/aosp/task/wiki/cases/docs，传 "all" 查全部。
    """
    kb_type_norm = (kb_type or "bug").strip().lower()
    try:
        if kb_type_norm == "all":
            sections = []
            for name, fn in _KB_STATS_DISPATCH.items():
                sections.append(f"===== {name} =====\n{fn()}")
            return "\n\n".join(sections)
        fn = _KB_STATS_DISPATCH.get(kb_type_norm)
        if fn is None:
            valid = "/".join(list(_KB_STATS_DISPATCH.keys()) + ["all"])
            return f"❌ 未知 kb_type「{kb_type}」。支持: {valid}"
        return fn()
    except Exception as e:
        logger.exception("kb_stats 异常 (type=%s)", kb_type_norm)
        return f"❌ 知识库统计失败: {e}"


# ── 工具 6: kb_reindex ────────────────────────────────────────────

@mcp.tool()
def kb_reindex(kb_type: str = "bug", model: str = "") -> str:
    """重建知识库向量索引。kb_type: bug/aosp/task/wiki/cases/docs。model: 向量化模型名。"""
    kb_type_norm = (kb_type or "bug").strip().lower()
    try:
        if kb_type_norm == "wiki":
            return "ℹ️ Wiki 知识库为纯 SQL LIKE 检索，无向量索引，无需重建。"
        fn = _KB_REINDEX_DISPATCH.get(kb_type_norm)
        if fn is None:
            valid = "/".join(_KB_REINDEX_DISPATCH.keys())
            return f"❌ 未知 kb_type「{kb_type}」。支持: {valid}"
        return fn(model or "")
    except Exception as e:
        logger.exception("kb_reindex 异常 (type=%s)", kb_type_norm)
        return f"❌ 重建索引失败: {e}"


# ── 工具 7: kb_review_cases ───────────────────────────────────────

@mcp.tool()
def kb_review_cases(limit: int = 20) -> str:
    """列出待审核的修复案例并审核。用于知识库 cases 的质量门控。"""
    try:
        return kb_cases.review_pending_cases(int(limit))
    except Exception as e:
        logger.exception("kb_review_cases 异常")
        return f"❌ 案例审核失败: {e}"


# ── 工具 8: kb_add_case ───────────────────────────────────────────

@mcp.tool()
def kb_add_case(
    bug_id: str,
    title: str,
    root_cause: str,
    fix_summary: str,
    fix_files: str = "",
    subsystem: str = "",
) -> str:
    """修复成功后将案例写入知识库 (kb_cases.sqlite)，自动审核。

    验证通过并提交代码后调用，将根因和修复方案记录为案例供后续同类 Bug 分析参考。
    subsystem 留空时自动从标题+文件路径推断。写入后自动 LLM 审核，
    通过则升为 approved 可被 kb_search(kb_type="cases") 检索。

    Args:
        bug_id: Bug ID（如 "82095"）
        title: Bug 标题
        root_cause: 根因简述（具体到模块/机制，一两句话）
        fix_summary: 修复简述（具体到改了什么，一两句话）
        fix_files: 修改的文件路径，逗号分隔（如 "frameworks/base/foo.java, system/bar.cpp"）
        subsystem: 子系统名（留空自动推断，如 "audio_subsystem"）
    """
    try:
        from bugflow.core import skill_feedback

        files_list = [f.strip() for f in (fix_files or "").split(",") if f.strip()]

        if not subsystem:
            subsystem = skill_feedback.infer_subsystem(title, files_list)

        case_id = kb_cases.add_case(
            bug_id=bug_id,
            title=title,
            subsystem=subsystem,
            root_cause=root_cause,
            fix_summary=fix_summary,
            fix_files=files_list,
        )

        if case_id is None:
            return "❌ 案例写入失败（详见 MCP server 日志）。"

        review_result = kb_cases.review_single_case(case_id)

        return (
            f"✅ 案例已入库 (bug #{bug_id}, {subsystem}, id={case_id})\n"
            f"{review_result}\n"
            f"审核通过后可通过 kb_search(kb_type=\"cases\", keyword=\"...\") 检索。"
        )
    except Exception as e:
        logger.exception("kb_add_case 异常")
        return f"❌ 案例写入失败: {e}"


# ── 工具 9: wiki_fetch_product ────────────────────────────────────

@mcp.tool()
def wiki_fetch_product(product: str) -> str:
    """拉取指定产品代码的 Wiki 页面信息。"""
    try:
        return wiki_mod.wiki(action="fetch_product", product=product)
    except wiki_mod.WikiError as e:
        return f"❌ Wiki 抓取失败: {e}"
    except Exception as e:
        logger.exception("wiki_fetch_product 异常")
        return f"❌ Wiki 抓取失败: {e}"


# ── 工具 10: batch_state_query ─────────────────────────────────────

@mcp.tool()
def batch_state_query(
    action: str = "load",
    bug_id: str = "",
    title: str = "",
    module: str = "",
    fix_status: str = "",
    phase: str = "",
    task_id: str = "",
    reproduce_commands: str = "",
    logcat_filter: str = "",
    gerrit_url: str = "",
    notes: str = "",
    retry_count: int = -1,
    date: str = "",
) -> str:
    """批量修复进度管理（断点续跑）。
    action: load(加载状态)/pending(查未完成)/update(更新bug状态)/reset(重置)。
    date: 指定日期 YYYY-MM-DD（默认今天），可查历史日期状态。
    update 可更新字段: title/module/fix_status/phase/task_id/reproduce_commands/logcat_filter/gerrit_url/notes/retry_count。
    retry_count: 失败次数，≥3 时调用方应升级 fix_status 为"需人工介入"（-1=不更新）。
    """
    target_date = date or datetime.now().strftime("%Y-%m-%d")
    try:
        if action == "load":
            state = batch_state.load_batch_state(target_date)
            summary = batch_state.state_summary(state)
            return f"批量状态 ({target_date}):\n" + json.dumps(
                summary, ensure_ascii=False, indent=2
            )
        elif action == "pending":
            state = batch_state.load_batch_state(target_date)
            summary = batch_state.state_summary(state)
            pending = summary.get("pending_bug_ids", [])
            if not pending:
                return f"✅ 无待处理 Bug（{target_date}）。"
            lines = [f"待处理 Bug ({len(pending)} 个, {target_date}):"]
            for bid in pending:
                rec = state.get("bugs", {}).get(bid, {})
                retry = rec.get("retry_count", 0)
                retry_tag = f" | 重试: {retry}/3" if retry > 0 else ""
                lines.append(
                    f"  #{bid}: {rec.get('title', '')} | "
                    f"状态: {rec.get('fix_status', '未记录')} | "
                    f"阶段: {rec.get('phase', '')}{retry_tag}"
                )
            return "\n".join(lines)
        elif action == "update":
            if not bug_id:
                return "❌ update 需要 bug_id 参数。"
            state = batch_state.load_batch_state(target_date)
            fields: dict = {}
            if title:
                fields["title"] = title
            if module:
                fields["module"] = module
            if fix_status:
                fields["fix_status"] = fix_status
            if phase:
                fields["phase"] = phase
            if task_id:
                fields["task_id"] = task_id
            if reproduce_commands:
                fields["reproduce_commands"] = reproduce_commands
            if logcat_filter:
                fields["logcat_filter"] = logcat_filter
            if gerrit_url:
                fields["gerrit_url"] = gerrit_url
            if notes:
                fields["notes"] = notes
            if retry_count >= 0:
                fields["retry_count"] = retry_count
            batch_state.update_bug_state(state, bug_id, **fields)
            result = batch_state.save_batch_state(state)
            return f"✅ Bug #{bug_id} 状态已更新并落盘 ({target_date}): {result}"
        elif action == "reset":
            result = batch_state.reset_batch_state(target_date)
            return f"✅ 已重置 {target_date} 批量状态: {result}"
        else:
            return f"❌ 未知 action「{action}」。支持: load/pending/update/reset。"
    except Exception as e:
        logger.exception("batch_state_query 异常 (action=%s)", action)
        return f"❌ 批量状态操作失败: {e}"


@mcp.tool()
def kb_sync(kb_type: str = "all", dry_run: bool = False) -> str:
    """从内网 85 共享同步知识库 SQLite 文件到本地配置目录。

    依赖 Windows 已缓存 85 服务器 SMB 凭证。首次使用请在资源管理器
    访问 \\\\192.168.0.85 并勾选"记住凭据"。

    Args:
        kb_type: all(默认同步全部) / bug / docs / aosp / wiki / tasks / cases
        dry_run: True 只预览状态不实际复制
    """
    kb_type_norm = (kb_type or "all").strip().lower()
    try:
        if dry_run:
            return _kb_sync.kb_sync_status()
        return _kb_sync.sync_kb(kb_type_norm)
    except Exception as e:
        logger.exception("kb_sync 异常 (type=%s, dry_run=%s)", kb_type_norm, dry_run)
        return f"❌ KB 同步失败: {e}"


if __name__ == "__main__":
    mcp.run(transport="stdio")
