# -*- coding: utf-8 -*-
"""Wiki 知识库搜索 — SQLite LIKE 查询。

与 core/kb.py（Bug KB）和 core/kb_task.py（Task KB）对称，
查 ~/.bugfix-flow/kb_wiki.sqlite 的 wiki_pages 表。

存储从 Wiki 抓取的产品页面信息，支持离线搜索。
用 scripts/build_kb_wiki.py 从 Wiki 批量抓取构建。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .config import _config_dir



def _kb_wiki_path() -> Path:
    """Wiki 知识库 SQLite 路径。"""
    return _config_dir() / "kb_wiki.sqlite"


def _init_db(conn: sqlite3.Connection) -> None:
    """初始化 wiki_pages 表（如不存在）。

    Schema 与 scripts/build_kb_wiki.py 保持一致。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS wiki_pages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            product         TEXT,
            title           TEXT,
            wikitext        TEXT,
            branches        TEXT,
            manifest_branch TEXT,
            manifest_xml    TEXT,
            download_commands TEXT,
            build_commands  TEXT,
            repos           TEXT,
            url             TEXT,
            updated_at      TEXT,
            UNIQUE(title)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_product ON wiki_pages(product)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_title ON wiki_pages(title)
    """)
    conn.commit()


def _search_kb_wiki(keyword: str, limit: int = 10) -> list[dict]:
    """搜索 Wiki 知识库。"""
    db_path = _kb_wiki_path()
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        _init_db(conn)

        keyword = (keyword or "").strip()
        if not keyword:
            rows = conn.execute(
                "SELECT * FROM wiki_pages ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

        like = f"%{keyword}%"
        terms = keyword.replace("，", " ").replace(",", " ").split()

        if len(terms) > 1:
            # AND 匹配 title/product
            conditions = " AND ".join(["(title LIKE ? OR product LIKE ?)" for _ in terms])
            params = []
            for t in terms:
                params.extend([f"%{t}%", f"%{t}%"])
            params.append(limit * 2)
            rows = conn.execute(
                f"SELECT * FROM wiki_pages WHERE {conditions} ORDER BY id DESC LIMIT ?", params
            ).fetchall()
            if not rows:
                # OR 回退
                or_conds = " OR ".join(["title LIKE ? OR product LIKE ? OR branches LIKE ?" for _ in terms])
                or_params = []
                for t in terms:
                    or_params.extend([f"%{t}%", f"%{t}%", f"%{t}%"])
                or_params.append(limit * 2)
                rows = conn.execute(
                    f"SELECT * FROM wiki_pages WHERE {or_conds} ORDER BY id DESC LIMIT ?", or_params
                ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM wiki_pages WHERE title LIKE ? OR product LIKE ? OR branches LIKE ? "
                "ORDER BY id DESC LIMIT ?",
                (like, like, like, limit * 2),
            ).fetchall()
    finally:
        conn.close()

    # 相关性排序
    kw = keyword.lower()
    def _score(r):
        title = (r.get("title") or "").lower()
        product = (r.get("product") or "").lower()
        if kw == product:
            return 0
        if kw in product:
            return 1
        if kw in title:
            return 2
        return 3

    rows_list = [dict(r) for r in rows]
    rows_list.sort(key=_score)
    return rows_list[:limit]


def search_kb_wiki_formatted(keyword: str = "", limit: int = 10) -> str:
    """搜索 Wiki 知识库并格式化输出。"""
    db_path = _kb_wiki_path()
    if not db_path.exists():
        return (
            "⚠️ Wiki 知识库未构建。请运行 scripts/build_kb_wiki.py 从 Wiki 抓取生成，"
            "或将 kb_wiki.sqlite 放到 ~/.bugfix-flow/ 目录。"
        )

    if not keyword:
        return "❌ 请提供 keyword（关键词）。"

    rows = _search_kb_wiki(keyword, limit)
    if not rows:
        return f"📭 Wiki 知识库中未找到匹配「{keyword}」的页面。\n建议：尝试更通用的关键词，或用 wiki_search(keyword=...) 实时搜索。"

    lines = [f"🔍 Wiki 知识库搜索「{keyword}」找到 {len(rows)} 条：\n"]
    for i, r in enumerate(rows, 1):
        title = r.get("title") or "?"
        product = r.get("product") or "?"
        lines.append(f"{i}. 📄 {title} (产品: {product})")

        if r.get("manifest_branch"):
            lines.append(f"   分支: `{r['manifest_branch']}`")
        if r.get("manifest_xml"):
            lines.append(f"   XML: `{r['manifest_xml']}`")
        if r.get("branches"):
            branches = r["branches"]
            if len(branches) > 100:
                branches = branches[:100] + "..."
            lines.append(f"   代码分支: {branches}")

        if r.get("download_commands"):
            cmds = r["download_commands"]
            if len(cmds) > 200:
                cmds = cmds[:200] + "..."
            lines.append(f"   拉取命令: {cmds}")

    lines.append(f"\n💡 需要更详细信息可用 wiki_fetch_product(product='{keyword}') 实时抓取。")
    return "\n".join(lines)


def kb_wiki_stats() -> str:
    """Wiki 知识库统计信息。"""
    db_path = _kb_wiki_path()
    if not db_path.exists():
        return "⚠️ Wiki 知识库未构建（kb_wiki.sqlite 不存在）。"

    conn = sqlite3.connect(str(db_path))
    _init_db(conn)

    total = conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]
    products = conn.execute(
        "SELECT product, COUNT(*) as cnt FROM wiki_pages WHERE product IS NOT NULL AND product != '' "
        "GROUP BY product ORDER BY cnt DESC LIMIT 15"
    ).fetchall()
    has_branches = conn.execute(
        "SELECT COUNT(*) FROM wiki_pages WHERE branches IS NOT NULL AND branches != ''"
    ).fetchone()[0]
    conn.close()

    lines = [f"# Wiki 知识库统计\n", f"- 总页面: {total:,} 条"]
    if total > 0:
        lines.append(f"- 含分支信息: {has_branches:,} ({100 * has_branches / total:.1f}%)")
    lines.append("\n## 产品 Top 15")
    for p, n in products:
        lines.append(f"- {p}: {n:,}")
    return "\n".join(lines)


def upsert_wiki_page(page_data: dict) -> None:
    """插入或更新 Wiki 页面到知识库（供 build_kb_wiki.py 用）。

    page_data: {product, title, wikitext, branches, manifest_branch,
                manifest_xml, download_commands, build_commands, repos, url}

    Schema 与 scripts/build_kb_wiki.py 的 _upsert_page 一致。
    """
    db_path = _kb_wiki_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    _init_db(conn)

    import time as _time
    conn.execute("""
        INSERT INTO wiki_pages
            (product, title, wikitext, branches, manifest_branch, manifest_xml,
             download_commands, build_commands, repos, url, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(title) DO UPDATE SET
            product=excluded.product,
            wikitext=excluded.wikitext,
            branches=excluded.branches,
            manifest_branch=excluded.manifest_branch,
            manifest_xml=excluded.manifest_xml,
            download_commands=excluded.download_commands,
            build_commands=excluded.build_commands,
            repos=excluded.repos,
            url=excluded.url,
            updated_at=excluded.updated_at
    """, (
        page_data.get("product", ""),
        page_data.get("title", ""),
        (page_data.get("wikitext") or "")[:50000],
        page_data.get("branches", ""),
        page_data.get("manifest_branch", ""),
        page_data.get("manifest_xml", ""),
        page_data.get("download_commands", ""),
        page_data.get("build_commands", ""),
        page_data.get("repos", ""),
        page_data.get("url", ""),
        _time.strftime("%Y-%m-%d %H:%M:%S"),
    ))
    conn.commit()
    conn.close()
