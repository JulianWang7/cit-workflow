# -*- coding: utf-8 -*-
"""禅道 Task 知识库搜索 — Hybrid 检索（SQL LIKE + 语义向量 RRF 融合）。

与 core/kb.py（Bug 知识库）对称，查 ~/.bugfix-flow/kb_tasks.sqlite 的 tasks 表。
Task 是需求/开发任务，同样有 Gerrit 链接和参考价值。

策略：分析问题前查 Bug KB + Task KB，看有无类似历史记录 → 参考解决方案。
检索：SQL LIKE 粗筛 + 向量语义重排 + RRF 融合，任意环节失败静默降级纯 LIKE。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import _config_dir
from .kb_base import (
    KBConfig,
    hybrid_search as _hybrid_search,
    reindex_kb as _reindex_base,
    sql_like_search as _sql_like_search,
    vector_coverage_section as _vec_coverage,
)


def _kb_task_path() -> Path:
    """Task 知识库 SQLite 路径：~/.bugfix-flow/kb_tasks.sqlite。"""
    return _config_dir() / "kb_tasks.sqlite"


_CFG = KBConfig(
    db_filename="kb_tasks.sqlite",
    emb_table="task_embeddings",
    source_table="tasks",
    id_col="rowid",
    domain_col="domain",
    title_col="name",
    order_by_col="task_id",
    or_fallback_cols=["name", "domain"],
    score_cols=["name", "description"],
    reindex_text_cols=["name", "description", "solution"],
    domain_exact_match=False,
)


def _search_kb_task_sql(keyword: str, domain: str = "", limit: int = 10) -> list[dict]:
    """SQL LIKE 搜索 Task 知识库（纯关键词路）。"""
    return _sql_like_search(_CFG, keyword, domain, limit, db_path=_kb_task_path())


def _search_kb_task(keyword: str, domain: str = "", limit: int = 10) -> list[dict]:
    """Hybrid 检索 Task 知识库：SQL LIKE 粗筛 + 向量语义重排 + RRF 融合。"""
    return _hybrid_search(_CFG, keyword, domain, limit, db_path=_kb_task_path())


def reindex_kb_task(model: str = "") -> str:
    """重建 Task 向量索引。从 tasks 表读 name+description+solution 向量化。"""
    return _reindex_base(_CFG, model, db_path=_kb_task_path())


def search_kb_task_formatted(keyword: str = "", domain: str = "", limit: int = 10) -> str:
    """搜索 Task 知识库并格式化输出。

    优先展示 status=done/closed 且有 solution 的条目。
    """
    db_path = _kb_task_path()
    if not db_path.exists():
        return (
            "⚠️ Task 知识库未构建。请运行 scripts/build_kb_tasks.py 生成，"
            "或将 kb_tasks.sqlite 放到 ~/.bugfix-flow/ 目录。"
        )

    if not keyword and not domain:
        return "❌ 请提供 keyword（关键词）或 domain（领域）。"

    rows = _search_kb_task(keyword, domain, limit)
    if not rows:
        return (
            f"📭 Task 知识库中未找到匹配「{keyword or domain}」的任务记录。\n"
            "建议：尝试更通用的关键词，或查 Bug 知识库 / Gerrit。"
        )

    lines = [f"📋 Task 知识库搜索「{keyword or domain}」找到 {len(rows)} 条相关任务：\n"]
    for i, r in enumerate(rows, 1):
        status = r.get("status", "?") or "?"
        name = (r.get("name") or "").replace("【", "[").replace("】", "]")
        if len(name) > 120:
            name = name[:117] + "..."

        lines.append(
            f"{i}. [{r.get('product_name', '?')}] Task#{r.get('task_id', '?')} {status} | {r.get('domain', '?') or '未分类'}"
        )
        lines.append(f"   {name}")

        # Gerrit 链接
        gerrit = r.get("gerrit_link") or ""
        if gerrit:
            lines.append(f"   🔗 Gerrit: {gerrit}")

        # 解决方案
        solution = r.get("solution") or ""
        if solution:
            sol = solution.strip()
            if len(sol) > 300:
                sol = sol[:297] + "..."
            lines.append(f"   💡 方案: {sol}")

        # 描述摘要
        desc = (r.get("description") or "").strip()
        if desc and not solution:
            desc_lines = [l.strip() for l in desc.split("\n") if l.strip() and not l.strip().startswith("\t")]
            brief = " ".join(desc_lines[:3])
            if len(brief) > 200:
                brief = brief[:197] + "..."
            if brief:
                lines.append(f"   📝 {brief}")

    lines.append(
        f"\n💡 优先关注 status=done/closed 的条目。"
        f"需要具体代码修改可查询对应 Gerrit 链接。"
    )
    return "\n".join(lines)


def kb_task_stats() -> str:
    """Task 知识库统计信息。"""
    db_path = _kb_task_path()
    if not db_path.exists():
        return "⚠️ Task 知识库未构建（kb_tasks.sqlite 不存在）。"

    conn = sqlite3.connect(str(db_path))
    total = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    domains = conn.execute(
        "SELECT domain, COUNT(*) as cnt FROM tasks WHERE domain IS NOT NULL AND domain != '' "
        "GROUP BY domain ORDER BY cnt DESC LIMIT 15"
    ).fetchall()

    statuses = conn.execute(
        "SELECT status, COUNT(*) FROM tasks GROUP BY status"
    ).fetchall()

    products = conn.execute(
        "SELECT product_name, COUNT(*) as cnt FROM tasks WHERE product_name IS NOT NULL "
        "GROUP BY product_name ORDER BY cnt DESC LIMIT 10"
    ).fetchall()

    # gerrit 覆盖率
    has_gerrit = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE has_gerrit = '是'"
    ).fetchone()[0]

    conn.close()

    lines = [f"# Task 知识库统计\n", f"- 总记录: {total:,} 条\n"]
    if total > 0:
        lines.append(f"- Gerrit 覆盖: {has_gerrit:,} ({100 * has_gerrit / total:.1f}%)\n")
    lines.append("## 状态分布")
    for s, n in statuses:
        lines.append(f"- {s}: {n:,}")
    lines.append("\n## 领域 Top 15")
    for d, n in domains:
        lines.append(f"- {d}: {n:,}")
    lines.append("\n## 产品 Top 10")
    for p, n in products:
        lines.append(f"- {p}: {n:,}")

    lines.append(_vec_coverage(_CFG, db_path, total, "kb(action='reindex', kb_type='task')"))

    return "\n".join(lines)
