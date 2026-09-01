# -*- coding: utf-8 -*-
"""禅道 Bug 知识库搜索 — Hybrid 检索（SQL LIKE + 语义向量 RRF 融合）。

从 YWAgent tools/kb_tools.py 移植，去掉 YWAgent 框架依赖（ToolRegistry），
改为 self-contained 模块。kb.sqlite 放在 ~/.bugfix-flow/kb.sqlite。

策略：分析问题前先查知识库有无类似 Bug → 参考解决方案 → 再搜源码。
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
    vector_coverage_section as _vec_coverage,
)


def _kb_path() -> Path:
    """知识库 SQLite 路径：~/.bugfix-flow/kb.sqlite。"""
    return _config_dir() / "kb.sqlite"


_CFG = KBConfig(
    db_filename="kb.sqlite",
    emb_table="bug_embeddings",
    source_table="bugs",
    id_col="bug_id",
    domain_col="domain",
    title_col="title",
    order_by_col="bug_id",
    or_fallback_cols=["title", "domain"],
    score_cols=["title", "description"],
    reindex_text_cols=["title", "description", "solution"],
    domain_exact_match=False,
)


def _search_kb(keyword: str, domain: str = "", limit: int = 10) -> list[dict]:
    """Hybrid 检索知识库：SQL LIKE 粗筛 + 向量语义重排 + RRF 融合。

    降级链：向量未配置/API不通/索引缺失 → 回退纯 SQL LIKE。
    """
    return _hybrid_search(_CFG, keyword, domain, limit, db_path=_kb_path())


def reindex_kb(model: str = "") -> str:
    """重建 Bug 向量索引。从 bugs 表读 title+description+solution 向量化。"""
    return _reindex_base(_CFG, model, db_path=_kb_path())


def search_kb_formatted(keyword: str = "", domain: str = "", limit: int = 10) -> str:
    """搜索知识库并格式化输出。

    优先展示 status=resolved/closed 且有 solution 的条目。
    """
    db_path = _kb_path()
    if not db_path.exists():
        return (
            "⚠️ 知识库未构建。请运行 build_kb.py 从禅道导出生成，"
            "或将 kb.sqlite 放到 ~/.bugfix-flow/ 目录。"
        )

    if not keyword and not domain:
        return "❌ 请提供 keyword（关键词）或 domain（领域）。"

    rows = _search_kb(keyword, domain, limit)
    if not rows:
        return (
            f"📭 知识库中未找到匹配「{keyword or domain}」的 Bug 记录。\n"
            "建议：尝试更通用的关键词，或直接查 Gerrit/禅道。"
        )

    lines = [f"🔍 知识库搜索「{keyword or domain}」找到 {len(rows)} 条相关 Bug：\n"]
    for i, r in enumerate(rows, 1):
        status = r.get("status", "?") or "?"
        resolution = r.get("resolution", "") or ""
        resolved = f" [{resolution}]" if resolution else ""
        title = (r.get("title") or "").replace("【", "[").replace("】", "]")
        if len(title) > 120:
            title = title[:117] + "..."

        lines.append(
            f"{i}. [{r.get('product_name', '?')}] #{r.get('bug_id', '?')} {status}{resolved} | {r.get('domain', '?') or '未分类'}"
        )
        lines.append(f"   {title}")

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
            lines.append(f"   💡 解决: {sol}")

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
        f"\n💡 优先关注 status=resolved/closed 的条目。"
        f"需要具体代码修改可查询对应 Gerrit 链接。"
    )
    return "\n".join(lines)


def kb_stats() -> str:
    """知识库统计信息。"""
    db_path = _kb_path()
    if not db_path.exists():
        return "⚠️ 知识库未构建（kb.sqlite 不存在）。"

    conn = sqlite3.connect(str(db_path))
    total = conn.execute("SELECT COUNT(*) FROM bugs").fetchone()[0]

    domains = conn.execute(
        "SELECT domain, COUNT(*) as cnt FROM bugs WHERE domain IS NOT NULL AND domain != '' "
        "GROUP BY domain ORDER BY cnt DESC LIMIT 15"
    ).fetchall()

    statuses = conn.execute(
        "SELECT status, COUNT(*) FROM bugs GROUP BY status"
    ).fetchall()

    products = conn.execute(
        "SELECT product_name, COUNT(*) as cnt FROM bugs WHERE product_name IS NOT NULL "
        "GROUP BY product_name ORDER BY cnt DESC LIMIT 10"
    ).fetchall()

    conn.close()

    lines = [f"# 知识库统计\n", f"- 总记录: {total:,} 条\n"]
    lines.append("## 状态分布")
    for s, n in statuses:
        lines.append(f"- {s}: {n:,}")
    lines.append("\n## 领域 Top 15")
    for d, n in domains:
        lines.append(f"- {d}: {n:,}")
    lines.append("\n## 产品 Top 10")
    for p, n in products:
        lines.append(f"- {p}: {n:,}")

    lines.append(_vec_coverage(_CFG, db_path, total, "kb(action='reindex')"))

    return "\n".join(lines)
