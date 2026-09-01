# -*- coding: utf-8 -*-
"""知识库共享检索层 — 消除 6 个 kb_*.py 模块的重复代码。

提取 SQL LIKE 搜索、Hybrid 检索（SQL+向量 RRF 融合）、向量覆盖率统计、
重建索引等共享逻辑。各域模块定义自己的 KBConfig 实例 + 格式化逻辑，
检索算法统一调用本模块函数。

适用模块: kb / kb_aosp / kb_task / kb_cases（sql_like_search）
          kb_docs（自定义 SQL，共享 hybrid_search/vector_coverage/reindex）
          kb_wiki 不参与（无向量，SQL 结构不同）
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import _config_dir
from .kb_vector import (
    build_embeddings,
    embed_text,
    embedding_stats,
    hybrid_rank,
    vector_search,
)


@dataclass
class KBConfig:
    """知识库检索配置 — 各 kb_* 模块定义自己的实例。

    字段含义:
        db_filename:     SQLite 文件名（如 "kb.sqlite"）
        emb_table:       向量索引表名（如 "bug_embeddings"）
        source_table:    源数据表名（如 "bugs"）
        id_col:          主键列名（如 "bug_id" / "id" / "rowid"）
        domain_col:      领域过滤列名（如 "domain" / "category" / "subsystem"）
        title_col:       标题列名，AND 匹配 + 相关性排序首选列
        order_by_col:    排序列名
        or_fallback_cols: OR 回退搜索列列表
        score_cols:      相关性排序列优先级列表
        reindex_text_cols: 重建向量索引时读取的文本列
        domain_exact_match:  no-keyword 路径用 WHERE domain = ?（True）或浏览全部（False）
    """

    db_filename: str
    emb_table: str
    source_table: str
    id_col: str
    domain_col: str
    title_col: str = "title"
    order_by_col: str = "id"
    or_fallback_cols: list[str] = field(default_factory=lambda: ["title"])
    score_cols: list[str] = field(default_factory=lambda: ["title"])
    reindex_text_cols: list[str] = field(default_factory=lambda: ["title"])
    domain_exact_match: bool = False


def kb_db_path(cfg: KBConfig) -> Path:
    """知识库 SQLite 路径: ~/.bugfix-flow/{db_filename}。"""
    return _config_dir() / cfg.db_filename


# ── SQL LIKE 搜索 ──────────────────────────────────────────────────

def sql_like_search(
    cfg: KBConfig,
    keyword: str,
    domain_val: str,
    limit: int = 10,
    init_db_fn: Callable[[sqlite3.Connection], None] | None = None,
    extra_where_sql: str = "",
    extra_where_params: list | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    """SQL LIKE 搜索（纯关键词路），返回匹配行列表。

    多词支持: 空格/逗号分隔 → 先 AND 匹配 title_col，无结果再 OR 回退到 or_fallback_cols。
    领域过滤: domain_val 非空时先在 SQL 中过滤（domain_exact_match）或后过滤。
    相关性排序: score_cols 优先级 → title 完全匹配 > title 包含 > 其他列包含。
    extra_where: 额外 WHERE 条件（如 kb_cases 的 status 过滤），应用于所有路径。
    db_path: 显式 DB 路径（域模块传入，支持测试 monkeypatch），None 则用 kb_db_path(cfg)。
    """
    db_path = db_path or kb_db_path(cfg)
    if not db_path.exists():
        return []

    extra_where_params = extra_where_params or []

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        if init_db_fn:
            init_db_fn(conn)

        keyword = (keyword or "").strip()
        domain_val = (domain_val or "").strip()

        if not keyword and not domain_val and not extra_where_sql:
            return []

        # 构建基础 WHERE（extra_where + domain 精确过滤）
        base_conds: list[str] = []
        base_params: list = []
        if extra_where_sql:
            base_conds.append(extra_where_sql)
            base_params.extend(extra_where_params)
        # domain_exact_match 模式: 加入 SQL WHERE（所有路径生效），跳过后过滤
        if cfg.domain_exact_match and domain_val:
            base_conds.append(f"{cfg.domain_col} = ?")
            base_params.append(domain_val)

        rows: list[dict] = []
        if keyword:
            like = f"%{keyword}%"
            terms = keyword.replace("，", " ").replace(",", " ").split()

            if len(terms) > 1:
                # AND 匹配 title_col
                conditions = " AND ".join([f"{cfg.title_col} LIKE ?" for _ in terms])
                and_params = [f"%{t}%" for t in terms]
                where_parts = list(base_conds)
                where_parts.insert(0, f"({conditions})")
                where = " AND ".join(where_parts)
                and_params.extend(base_params)
                and_params.append(limit * 2)
                cur = conn.execute(
                    f"SELECT * FROM {cfg.source_table} WHERE {where} "
                    f"ORDER BY {cfg.order_by_col} DESC LIMIT ?",
                    and_params,
                )
                rows = [dict(r) for r in cur.fetchall()]

                if not rows:
                    # OR 回退: 任意词匹配 or_fallback_cols
                    per_term = " OR ".join([f"{c} LIKE ?" for c in cfg.or_fallback_cols])
                    or_conds = " OR ".join([per_term for _ in terms])
                    or_params: list = []
                    for t in terms:
                        or_params.extend([f"%{t}%" for _ in cfg.or_fallback_cols])
                    where_parts = list(base_conds)
                    where_parts.insert(0, f"({or_conds})")
                    where = " AND ".join(where_parts)
                    or_params.extend(base_params)
                    or_params.append(limit * 2)
                    cur = conn.execute(
                        f"SELECT * FROM {cfg.source_table} WHERE {where} "
                        f"ORDER BY {cfg.order_by_col} DESC LIMIT ?",
                        or_params,
                    )
                    rows = [dict(r) for r in cur.fetchall()]
            else:
                # 单词: OR 匹配 or_fallback_cols
                or_cols = " OR ".join([f"{c} LIKE ?" for c in cfg.or_fallback_cols])
                single_params: list = [like] * len(cfg.or_fallback_cols)
                if base_conds:
                    where = f"({or_cols}) AND {' AND '.join(base_conds)}"
                    single_params.extend(base_params)
                else:
                    where = or_cols
                single_params.append(limit * 2)
                cur = conn.execute(
                    f"SELECT * FROM {cfg.source_table} WHERE {where} "
                    f"ORDER BY {cfg.order_by_col} DESC LIMIT ?",
                    single_params,
                )
                rows = [dict(r) for r in cur.fetchall()]
        else:
            # 无关键词: domain 精确匹配（已在 base_conds）或浏览全部
            conds = list(base_conds)
            params = list(base_params)
            where = " AND ".join(conds) if conds else ""
            sql = f"SELECT * FROM {cfg.source_table}"
            if where:
                sql += f" WHERE {where}"
            sql += f" ORDER BY {cfg.order_by_col} DESC LIMIT ?"
            params.append(limit)
            cur = conn.execute(sql, params)
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    # 领域后过滤（case-insensitive contains）— domain_exact_match 时跳过（SQL 已精确过滤）
    if domain_val and rows and not cfg.domain_exact_match:
        dv_lower = domain_val.lower()
        rows = [
            r for r in rows
            if (r.get(cfg.domain_col) or "").lower().find(dv_lower) >= 0
        ]

    # 相关性排序
    if keyword:
        rows.sort(key=lambda r: _relevance_score(cfg, r, keyword))

    return rows[:limit]


def _relevance_score(cfg: KBConfig, row: dict, keyword: str) -> int:
    """按 score_cols 优先级计算相关性分数。

    score 0 = score_cols[0] 完全匹配
    score 1 = score_cols[0] 包含
    score 2..n = score_cols[1..n] 包含
    score n+1 = 无匹配
    """
    kw = keyword.lower()
    for i, col in enumerate(cfg.score_cols):
        val = (row.get(col) or "").lower() if row.get(col) else ""
        if i == 0 and kw == val:
            return 0
        if kw in val:
            return 1 if i == 0 else i + 1
    return len(cfg.score_cols) + 1


# ── Hybrid 检索 ────────────────────────────────────────────────────

def hybrid_search(
    cfg: KBConfig,
    keyword: str,
    domain_val: str,
    limit: int,
    init_db_fn: Callable[[sqlite3.Connection], None] | None = None,
    extra_where_sql: str = "",
    extra_where_params: list | None = None,
    sql_search_fn: Callable | None = None,
    vec_filter_col: str = "",
    vec_filter_val: str = "",
    db_path: Path | None = None,
) -> list[dict]:
    """Hybrid 检索: SQL LIKE 粗筛 + 向量语义重排 + RRF 融合。

    降级链: 向量未配置/API不通/索引缺失 → 回退纯 SQL。
    sql_search_fn: 自定义 SQL 搜索函数（如 kb_docs 的双过滤），
                   None 则用标准 sql_like_search。
    vec_filter_col/val: 向量结果后过滤（如 kb_cases 按 status 过滤）。
    db_path: 显式 DB 路径（域模块传入，支持测试 monkeypatch），None 则用 kb_db_path(cfg)。
    """
    db_path = db_path or kb_db_path(cfg)
    candidate_limit = limit * 3

    if sql_search_fn:
        sql_rows = sql_search_fn(keyword, domain_val, candidate_limit)
    else:
        sql_rows = sql_like_search(
            cfg, keyword, domain_val, candidate_limit,
            init_db_fn=init_db_fn,
            extra_where_sql=extra_where_sql,
            extra_where_params=extra_where_params,
            db_path=db_path,
        )

    # 无关键词 → 纯 SQL（向量需要 query 文本）
    if not keyword or not keyword.strip():
        return sql_rows[:limit]

    vec, model = embed_text(keyword)
    if vec is None:
        return sql_rows[:limit]

    vec_rows = vector_search(
        db_path,
        cfg.emb_table,
        cfg.source_table,
        vec,
        model,
        id_col=cfg.id_col,
        domain_col=cfg.domain_col,
        domain_val=domain_val,
        limit=candidate_limit,
    )
    if not vec_rows:
        return sql_rows[:limit]

    # 向量结果后过滤（kb_cases: 按 status 过滤混入的非 approved 行）
    if vec_filter_col and vec_filter_val:
        vec_rows = [r for r in vec_rows if r.get(vec_filter_col) == vec_filter_val]
        if not vec_rows:
            return sql_rows[:limit]

    return hybrid_rank(
        sql_rows, vec_rows, keyword, cfg.id_col, limit, title_col=cfg.title_col,
    )


# ── 向量索引覆盖率统计 ─────────────────────────────────────────────

def vector_coverage_section(
    cfg: KBConfig, db_path: Path, total: int, reindex_hint: str = ""
) -> str:
    """向量索引覆盖率统计段落（供各模块 *_stats 函数调用）。

    返回多行文本，包含覆盖率百分比和未索引警告。
    reindex_hint: 未构建时的提示命令（各模块 reindex 调用格式不同）。
    """
    vec_info = embedding_stats(db_path, cfg.emb_table, total)
    lines = ["\n## 向量索引"]
    if vec_info == "未构建":
        hint = reindex_hint or "运行 reindex 启用语义检索"
        lines.append(f"- 未构建（可选，{hint}）")
    else:
        lines.append(f"- {vec_info}")
        if total > 0:
            n_indexed = 0
            try:
                conn2 = sqlite3.connect(str(db_path))
                n_indexed = conn2.execute(
                    f"SELECT COUNT(*) FROM {cfg.emb_table}"
                ).fetchone()[0]
                conn2.close()
            except sqlite3.OperationalError:
                pass
            if n_indexed < total:
                hint = reindex_hint or "reindex"
                lines.append(f"- ⚠️ {total - n_indexed} 条未索引，建议 {hint}")
    return "\n".join(lines)


# ── 重建向量索引 ───────────────────────────────────────────────────

def reindex_kb(cfg: KBConfig, model: str = "", db_path: Path | None = None) -> str:
    """重建向量索引。从源表读 reindex_text_cols 向量化。"""
    db_path = db_path or kb_db_path(cfg)
    return build_embeddings(
        db_path,
        cfg.emb_table,
        cfg.source_table,
        cfg.id_col,
        cfg.reindex_text_cols,
        model=model,
    )
