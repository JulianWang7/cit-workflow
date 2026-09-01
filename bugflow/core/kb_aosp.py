# -*- coding: utf-8 -*-
"""AOSP 知识库搜索 — Hybrid 检索（SQL LIKE + 语义向量 RRF 融合）。

与 core/kb_task.py（Task 知识库）对称，查 ~/.bugfix-flow/kb_aosp.sqlite 的
aosp_knowledge 表。存储从 E:\\文档 导入的架构追踪文档、bug 复盘、实现配方等。

策略：subsystem skill 分析问题前查本库，看有无相关架构追踪/案例复盘 → 参考分析路径。
检索：SQL LIKE 粗筛 + 向量语义重排 + RRF 融合，任意环节失败静默降级纯 LIKE。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from .config import _config_dir
from .kb_base import (
    KBConfig,
    hybrid_search as _hybrid_search,
    reindex_kb as _reindex_base,
    sql_like_search as _sql_like_search,
    vector_coverage_section as _vec_coverage,
)


def _kb_aosp_path() -> Path:
    """AOSP 知识库 SQLite 路径：~/.bugfix-flow/kb_aosp.sqlite。"""
    return _config_dir() / "kb_aosp.sqlite"


_CFG = KBConfig(
    db_filename="kb_aosp.sqlite",
    emb_table="aosp_embeddings",
    source_table="aosp_knowledge",
    id_col="id",
    domain_col="category",
    title_col="title",
    order_by_col="created_at",
    or_fallback_cols=["title", "category", "tags"],
    score_cols=["title", "category", "tags"],
    reindex_text_cols=["title", "content"],
    domain_exact_match=True,
)

# aosp_knowledge 表建表语句（与 scripts/build_kb_aosp.py 的 _create_table 一致）
_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS aosp_knowledge (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    category    TEXT NOT NULL,
    subcategory TEXT,
    source      TEXT,
    source_id   TEXT,
    content     TEXT NOT NULL,
    tags        TEXT,
    platform    TEXT,
    created_at  REAL NOT NULL,
    UNIQUE(title)
)
"""


def _init_db(conn: sqlite3.Connection) -> None:
    """幂等建表 + 索引（读取路径也会调用，确保新库可工作）。"""
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON aosp_knowledge(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_title ON aosp_knowledge(title)")
    conn.commit()


def _search_kb_aosp_sql(keyword: str, category: str = "", limit: int = 10) -> list[dict]:
    """SQL LIKE 搜索 AOSP 知识库（纯关键词路）。"""
    return _sql_like_search(_CFG, keyword, category, limit, init_db_fn=_init_db, db_path=_kb_aosp_path())


def _search_kb_aosp(keyword: str, category: str = "", limit: int = 10) -> list[dict]:
    """Hybrid 检索 AOSP 知识库：SQL LIKE 粗筛 + 向量语义重排 + RRF 融合。"""
    return _hybrid_search(_CFG, keyword, category, limit, init_db_fn=_init_db, db_path=_kb_aosp_path())


def reindex_kb_aosp(model: str = "") -> str:
    """重建 AOSP 知识库向量索引。从 aosp_knowledge 表读 title+content 向量化。"""
    return _reindex_base(_CFG, model, db_path=_kb_aosp_path())


def search_kb_aosp_formatted(
    keyword: str = "", category: str = "", limit: int = 10
) -> str:
    """搜索 AOSP 知识库并格式化输出。

    返回架构追踪/案例复盘/实现配方文档，辅助定位根因。
    """
    db_path = _kb_aosp_path()
    if not db_path.exists():
        return (
            "⚠️ AOSP 知识库未构建。请运行 scripts/build_kb_aosp.py 导入文档，"
            "生成 kb_aosp.sqlite 到 ~/.bugfix-flow/ 目录。"
        )

    if not keyword and not category:
        return "❌ 请提供 keyword（关键词）或 domain/category（分类）。"

    rows = _search_kb_aosp(keyword, category, limit)
    if not rows:
        return (
            f"📭 AOSP 知识库中未找到匹配「{keyword or category}」的文档。\n"
            "建议：尝试更通用的关键词，或查 Bug 知识库 / Task 知识库。"
        )

    lines = [f"🔍 AOSP 知识库搜索「{keyword or category}」找到 {len(rows)} 条：\n"]
    for i, r in enumerate(rows, 1):
        title = r.get("title") or ""
        if len(title) > 120:
            title = title[:117] + "..."

        cat = r.get("category") or "未分类"
        sub = r.get("subcategory") or ""
        src = r.get("source") or ""
        plat = r.get("platform") or "通用"

        lines.append(f"{i}. 📄 [{cat}] {title}")

        meta_parts = []
        if sub:
            meta_parts.append(f"子类: {sub}")
        if src:
            meta_parts.append(f"来源: {src}")
        meta_parts.append(f"平台: {plat}")
        lines.append(f"   {' | '.join(meta_parts)}")

        # 内容摘要（前 300 字符）
        content = (r.get("content") or "").strip()
        if content:
            # 取前几个非空段落
            paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
            brief = paragraphs[0] if paragraphs else ""
            if len(brief) > 300:
                brief = brief[:297] + "..."
            if brief:
                lines.append(f"   📝 {brief}")

        # 标签
        tags = r.get("tags") or ""
        if tags:
            lines.append(f"   🏷️ {tags}")

    lines.append(
        "\n💡 命中的文档包含架构追踪/案例复盘/实现配方，"
        "参考其分析路径辅助定位根因。"
    )
    return "\n".join(lines)


def kb_aosp_stats() -> str:
    """AOSP 知识库统计信息。"""
    db_path = _kb_aosp_path()
    if not db_path.exists():
        return "⚠️ AOSP 知识库未构建（kb_aosp.sqlite 不存在）。"

    conn = sqlite3.connect(str(db_path))
    _init_db(conn)
    total = conn.execute("SELECT COUNT(*) FROM aosp_knowledge").fetchone()[0]

    categories = conn.execute(
        "SELECT category, COUNT(*) as cnt FROM aosp_knowledge "
        "GROUP BY category ORDER BY cnt DESC"
    ).fetchall()

    sources = conn.execute(
        "SELECT source, COUNT(*) as cnt FROM aosp_knowledge "
        "WHERE source IS NOT NULL AND source != '' "
        "GROUP BY source ORDER BY cnt DESC"
    ).fetchall()

    subcats = conn.execute(
        "SELECT subcategory, COUNT(*) as cnt FROM aosp_knowledge "
        "WHERE subcategory IS NOT NULL AND subcategory != '' "
        "GROUP BY subcategory ORDER BY cnt DESC LIMIT 15"
    ).fetchall()

    conn.close()

    lines = [f"# AOSP 知识库统计\n", f"- 总记录: {total:,} 条\n"]

    if total > 0:
        lines.append("\n## 分类分布")
        for c, n in categories:
            lines.append(f"- {c}: {n:,}")

        if sources:
            lines.append("\n## 来源分布")
            for s, n in sources:
                lines.append(f"- {s}: {n:,}")

        if subcats:
            lines.append("\n## 子类 Top 15")
            for sc, n in subcats:
                lines.append(f"- {sc}: {n:,}")

    lines.append(_vec_coverage(_CFG, db_path, total, "kb(action='reindex', kb_type='aosp_knowledge')"))

    return "\n".join(lines)


def upsert_aosp_doc(doc_data: dict) -> None:
    """插入或更新 AOSP 知识库文档（供 build_kb_aosp.py 用）。

    doc_data: {title, category, subcategory, source, source_id, content, tags, platform}

    Schema 与 scripts/build_kb_aosp.py 的 _upsert_doc 一致。
    """
    db_path = _kb_aosp_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    _init_db(conn)

    conn.execute("""
        INSERT INTO aosp_knowledge
            (title, category, subcategory, source, source_id, content, tags, platform, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(title) DO UPDATE SET
            category=excluded.category,
            subcategory=excluded.subcategory,
            source=excluded.source,
            source_id=excluded.source_id,
            content=excluded.content,
            tags=excluded.tags,
            platform=excluded.platform,
            created_at=excluded.created_at
    """, (
        doc_data.get("title", ""),
        doc_data.get("category", ""),
        doc_data.get("subcategory"),
        doc_data.get("source", ""),
        doc_data.get("source_id", ""),
        (doc_data.get("content") or "")[:50000],
        doc_data.get("tags"),
        doc_data.get("platform", "通用"),
        time.time(),
    ))
    conn.commit()
    conn.close()
