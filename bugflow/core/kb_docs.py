# -*- coding: utf-8 -*-
"""文档知识库搜索 — Hybrid 检索（SQL LIKE + 语义向量 RRF 融合）。

与 bugflow/core/kb_aosp.py 对称，查 ~/.bugfix-flow/kb_docs.sqlite 的 docs 表。
存储从 SMB 文件服务器提取的 24K+ 文档（PDF/Word/PPT → 纯文本），按 tier
分层（P0 自有知识 / P1 平台参考 / P2 期刊项目）。

策略：分析问题时查本库，参考历史文档/模块手册/平台指南辅助定位根因。
检索：SQL LIKE 粗筛 + 向量语义重排 + RRF 融合，任意环节失败静默降级纯 LIKE。
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from .config import _config_dir
from .kb_base import (
    KBConfig,
    hybrid_search as _hybrid_search,
    reindex_kb as _reindex_base,
    vector_coverage_section as _vec_coverage,
)

logger = logging.getLogger("bugflow.core.kb_docs")

def _kb_docs_path() -> Path:
    """文档知识库 SQLite 路径：~/.bugfix-flow/kb_docs.sqlite。"""
    return _config_dir() / "kb_docs.sqlite"


_CFG = KBConfig(
    db_filename="kb_docs.sqlite",
    emb_table="docs_embeddings",
    source_table="docs",
    id_col="id",
    domain_col="tier",
    title_col="title",
    order_by_col="created_at",
    or_fallback_cols=["title", "category", "content"],
    score_cols=["title", "category"],
    reindex_text_cols=["title", "content"],
    domain_exact_match=False,
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS docs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    tier        TEXT NOT NULL,
    category    TEXT NOT NULL,
    source      TEXT,
    corpus_file TEXT,
    ext         TEXT,
    content     TEXT NOT NULL,
    chars       INTEGER DEFAULT 0,
    doc_date    TEXT,
    hash        TEXT,
    created_at  REAL NOT NULL,
    UNIQUE(hash)
)
"""


def _init_db(conn: sqlite3.Connection) -> None:
    """幂等建表 + 索引（读取路径也会调用，确保新库可工作）。"""
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tier ON docs(tier)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_category ON docs(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_title ON docs(title)")
    conn.commit()


def _search_kb_docs_sql(
    keyword: str, tier: str = "", category: str = "", limit: int = 10
) -> list[dict]:
    """SQL LIKE 搜索文档知识库（纯关键词路），返回匹配的文档列表。

    多词支持：空格/逗号分隔的多个词，先 AND 匹配 title，无结果再 OR 回退到
    title/category/content。
    过滤：tier（P0/P1/P2）和 category 非空时在结果中二次过滤。
    相关性排序：title 完全匹配 > title 包含 > category 匹配 > 其他。

    注意：no-keyword 浏览模式支持 tier+category 双过滤（与标准 sql_like_search
    的单 domain 过滤不同），因此保留自定义实现。
    """
    db_path = _kb_docs_path()
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        _init_db(conn)

        keyword = (keyword or "").strip()
        tier = (tier or "").strip()
        category = (category or "").strip()

        if not keyword and not tier and not category:
            return []

        rows: list[dict] = []
        if keyword:
            like = f"%{keyword}%"
            terms = keyword.replace("，", " ").replace(",", " ").split()
            if len(terms) > 1:
                # 先用 ALL terms AND 匹配 title
                conditions = " AND ".join(["title LIKE ?" for _ in terms])
                params: list = [f"%{t}%" for t in terms] + [limit * 2]
                cur = conn.execute(
                    f"SELECT * FROM docs WHERE {conditions} "
                    "ORDER BY created_at DESC LIMIT ?",
                    params,
                )
                rows = [dict(r) for r in cur.fetchall()]
                if not rows:
                    # AND 无结果，回退 OR（任意词匹配 title 或 category 或 content）
                    or_conds = " OR ".join(
                        ["title LIKE ? OR category LIKE ? OR content LIKE ?" for _ in terms]
                    )
                    or_params: list = []
                    for t in terms:
                        or_params.extend([f"%{t}%", f"%{t}%", f"%{t}%"])
                    or_params.append(limit * 2)
                    cur = conn.execute(
                        f"SELECT * FROM docs WHERE {or_conds} "
                        "ORDER BY created_at DESC LIMIT ?",
                        or_params,
                    )
                    rows = [dict(r) for r in cur.fetchall()]
            else:
                cur = conn.execute(
                    "SELECT * FROM docs "
                    "WHERE title LIKE ? OR category LIKE ? OR content LIKE ? "
                    "ORDER BY created_at DESC LIMIT ?",
                    (like, like, like, limit * 2),
                )
                rows = [dict(r) for r in cur.fetchall()]
        else:
            # 无关键词，按 tier/category 浏览（双过滤）
            conds: list[str] = []
            params2: list = []
            if tier:
                conds.append("tier = ?")
                params2.append(tier)
            if category:
                conds.append("category LIKE ?")
                params2.append(f"%{category}%")
            where = " AND ".join(conds) if conds else "1=1"
            params2.append(limit)
            cur = conn.execute(
                f"SELECT * FROM docs WHERE {where} "
                "ORDER BY created_at DESC LIMIT ?",
                params2,
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    # tier/category 二次过滤（针对关键词路径的结果）
    if tier and rows:
        tier_upper = tier.upper()
        rows = [r for r in rows if (r.get("tier") or "").upper() == tier_upper]
    if category and rows:
        cat_lower = category.lower()
        rows = [
            r
            for r in rows
            if (r.get("category") or "").lower().find(cat_lower) >= 0
        ]

    # 按相关性排序
    if keyword:
        kw = keyword.lower()

        def _score(r):
            title = (r.get("title") or "").lower()
            cat = (r.get("category") or "").lower() if r.get("category") else ""
            if kw == title:
                return 0
            if kw in title:
                return 1
            if kw in cat:
                return 2
            return 3

        rows.sort(key=_score)

    return rows[:limit]


def _search_kb_docs(
    keyword: str, tier: str = "", category: str = "", limit: int = 10
) -> list[dict]:
    """Hybrid 检索文档知识库：SQL LIKE 粗筛 + 向量语义重排 + RRF 融合。

    降级链：向量未配置/API不通/索引缺失 → 回退纯 _search_kb_docs_sql。
    """
    return _hybrid_search(
        _CFG, keyword, tier, limit,
        sql_search_fn=lambda kw, dv, lim: _search_kb_docs_sql(kw, dv, category, lim),
        db_path=_kb_docs_path(),
    )


def reindex_kb_docs(model: str = "") -> str:
    """重建文档知识库向量索引。从 docs 表读 title+content 向量化。"""
    return _reindex_base(_CFG, model, db_path=_kb_docs_path())


def search_kb_docs_formatted(
    keyword: str = "", tier: str = "", category: str = "", limit: int = 10
) -> str:
    """搜索文档知识库并格式化输出。

    返回模块手册/平台指南/技术期刊等文档，辅助定位根因。
    tier 过滤：P0（自有知识）/ P1（平台参考）/ P2（期刊项目）。
    """
    db_path = _kb_docs_path()
    if not db_path.exists():
        return (
            "⚠️ 文档知识库未构建。请运行 scripts/build_kb_docs.py 导入文档，"
            "生成 kb_docs.sqlite 到 ~/.bugfix-flow/ 目录。"
        )

    if not keyword and not tier and not category:
        return "❌ 请提供 keyword（关键词）或 tier（P0/P1/P2）或 category（分类）。"

    rows = _search_kb_docs(keyword, tier, category, limit)
    if not rows:
        return (
            f"📭 文档知识库中未找到匹配「{keyword or tier or category}」的文档。\n"
            "建议：尝试更通用的关键词，或查 Bug 知识库 / AOSP 知识库。"
        )

    lines = [f"🔍 文档知识库搜索「{keyword or tier or category}」找到 {len(rows)} 条：\n"]
    for i, r in enumerate(rows, 1):
        title = r.get("title") or ""
        if len(title) > 120:
            title = title[:117] + "..."

        t = r.get("tier") or "?"
        cat = r.get("category") or "未分类"
        ext = r.get("ext") or ""

        lines.append(f"{i}. 📄 [{t}] {title}")

        meta_parts = [f"分类: {cat}"]
        if ext:
            meta_parts.append(f"格式: {ext}")
        chars = r.get("chars") or 0
        if chars:
            meta_parts.append(f"字符: {chars:,}")
        lines.append(f"   {' | '.join(meta_parts)}")

        # 内容摘要（前 300 字符）
        content = (r.get("content") or "").strip()
        if content:
            paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
            brief = paragraphs[0] if paragraphs else ""
            if len(brief) > 300:
                brief = brief[:297] + "..."
            if brief:
                lines.append(f"   📝 {brief}")

        # 原始路径
        src = r.get("source") or ""
        if src:
            if len(src) > 100:
                src = "..." + src[-97:]
            lines.append(f"   📎 {src}")

    tier_desc = {
        "P0": "自有知识（项目/内部文档）",
        "P1": "平台/厂商参考",
        "P2": "期刊/项目文档",
    }
    tier_hint = ""
    if rows and rows[0].get("tier"):
        t0 = rows[0]["tier"]
        tier_hint = f"\n💡 当前结果以 {t0}（{tier_desc.get(t0, '')}）为主。"
    lines.append(
        f"\n💡 命中的文档包含模块手册/平台指南/技术期刊，"
        f"参考其内容辅助定位根因。{tier_hint}"
    )
    return "\n".join(lines)


def kb_docs_stats() -> str:
    """文档知识库统计信息。"""
    db_path = _kb_docs_path()
    if not db_path.exists():
        return "⚠️ 文档知识库未构建（kb_docs.sqlite 不存在）。"

    conn = sqlite3.connect(str(db_path))
    _init_db(conn)
    total = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]

    tiers = conn.execute(
        "SELECT tier, COUNT(*) as cnt FROM docs GROUP BY tier ORDER BY cnt DESC"
    ).fetchall()

    categories = conn.execute(
        "SELECT category, COUNT(*) as cnt FROM docs "
        "GROUP BY category ORDER BY cnt DESC LIMIT 20"
    ).fetchall()

    exts = conn.execute(
        "SELECT ext, COUNT(*) as cnt FROM docs "
        "WHERE ext IS NOT NULL AND ext != '' "
        "GROUP BY ext ORDER BY cnt DESC LIMIT 10"
    ).fetchall()

    conn.close()

    lines = [f"# 文档知识库统计\n", f"- 总记录: {total:,} 条\n"]

    if total > 0:
        lines.append("\n## 层级分布")
        for t, n in tiers:
            lines.append(f"- {t}: {n:,}")

        if categories:
            lines.append("\n## 分类分布 Top 20")
            for c, n in categories:
                lines.append(f"- {c}: {n:,}")

        if exts:
            lines.append("\n## 文件格式分布")
            for e, n in exts:
                lines.append(f"- {e}: {n:,}")

    lines.append(_vec_coverage(_CFG, db_path, total, "kb_reindex(kb_type='docs')"))

    return "\n".join(lines)


def upsert_doc(doc_data: dict) -> None:
    """插入或更新文档知识库记录（供 build_kb_docs.py 用）。

    doc_data: {title, tier, category, source, corpus_file, ext, content, chars, doc_date, hash}
    """
    db_path = _kb_docs_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    _init_db(conn)

    conn.execute("""
        INSERT INTO docs
            (title, tier, category, source, corpus_file, ext, content,
             chars, doc_date, hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(hash) DO UPDATE SET
            title=excluded.title,
            tier=excluded.tier,
            category=excluded.category,
            source=excluded.source,
            corpus_file=excluded.corpus_file,
            ext=excluded.ext,
            content=excluded.content,
            chars=excluded.chars,
            doc_date=excluded.doc_date,
            created_at=excluded.created_at
    """, (
        doc_data.get("title", ""),
        doc_data.get("tier", ""),
        doc_data.get("category", ""),
        doc_data.get("source", ""),
        doc_data.get("corpus_file", ""),
        doc_data.get("ext", ""),
        (doc_data.get("content") or "")[:50000],
        doc_data.get("chars", 0),
        doc_data.get("doc_date", ""),
        doc_data.get("hash", ""),
        time.time(),
    ))
    conn.commit()
    conn.close()
