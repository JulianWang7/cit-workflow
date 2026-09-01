# -*- coding: utf-8 -*-
"""知识库向量检索共享 helper — 向量化 + 余弦相似度 + RRF 融合 + 通用建索引/检索。

零新依赖（纯 urllib + json + math），复用 core.config.load_llm_config() 的
embedding_base_url / embedding_api_key / embedding_model 配置。

被 core/kb.py 和 core/kb_task.py 共用，也预留给 core/kb_wiki.py（第一期不改）。

设计红线：任何环节失败（未配置 / API 不通 / 索引缺失 / model 不一致）都静默
降级到纯 LIKE 搜索，绝不抛异常阻断现有流程。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any

from .config import load_llm_config

logger = logging.getLogger("core.kb_vector")

# ── 配置 ──────────────────────────────────────────────────────────

DEFAULT_EMBEDDING_MODEL = "embedding-3"
DEFAULT_DIMENSIONS = 512  # 智谱 embedding-3 支持 dimensions 参数，512 维够用且快
EMBED_BATCH_SIZE = 32  # 单批最大条数（降低以减少 429 限流）
RRF_K = 60  # RRF 经验常数
MAX_TEXT_LEN = 8000  # 截断防超长
EMBED_MAX_RETRIES = 5  # 429/超时 退避重试次数


def _embedding_config() -> dict[str, str]:
    """从 llm.yaml 读 embedding 配置，缺 base_url/api_key 返回部分 dict。"""
    cfg = load_llm_config()
    return {
        "base_url": cfg.get("embedding_base_url", ""),
        "api_key": cfg.get("embedding_api_key", ""),
        "model": cfg.get("embedding_model", DEFAULT_EMBEDDING_MODEL),
        "dimensions": str(cfg.get("embedding_dimensions", DEFAULT_DIMENSIONS)),
    }


# ── 向量化 ────────────────────────────────────────────────────────


def _embed_batch_raw(
    texts: list[str],
    model: str,
    base_url: str,
    api_key: str,
    dimensions: int = DEFAULT_DIMENSIONS,
    timeout: int = 60,
) -> list[list[float]] | None:
    """调 /embeddings 端点批量向量化。失败返回 None（不抛异常）。

    端点路径：{base_url}/embeddings （智谱 paas/v4 + /embeddings）。
    遇 429/超时自动退避重试（指数退避，最多 EMBED_MAX_RETRIES 次）。
    """
    import urllib.error as _ue

    url = f"{base_url.rstrip('/')}/embeddings"
    payload: dict[str, Any] = {"model": model, "input": texts}
    if dimensions:
        payload["dimensions"] = dimensions
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    for attempt in range(EMBED_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
            return [item["embedding"] for item in data["data"]]
        except _ue.HTTPError as e:
            # 429/5xx → 退避重试；其余 HTTP 错误（404/400 等）立即降级
            if e.code == 429 or e.code >= 500:
                wait = 2 ** attempt  # 1, 2, 4, 8, 16s
                logger.warning(
                    "embedding API 第 %d/%d 次重试 (HTTP %d), 等 %ds: %s",
                    attempt + 1, EMBED_MAX_RETRIES, e.code, wait, e,
                )
                time.sleep(wait)
                continue
            logger.warning("embedding API HTTP %d，降级纯关键词: %s", e.code, e)
            return None
        except (TimeoutError, OSError) as e:
            # 网络超时/连接失败 → 退避重试
            wait = 2 ** attempt  # 1, 2, 4, 8, 16s
            logger.warning(
                "embedding API 第 %d/%d 次重试 (超时/网络), 等 %ds: %s",
                attempt + 1, EMBED_MAX_RETRIES, wait, e,
            )
            time.sleep(wait)
            continue
        except (KeyError, ValueError) as e:
            logger.warning("embedding API 响应解析失败，降级纯关键词: %s", e)
            return None
    logger.warning("embedding API 重试 %d 次后仍失败，该批降级", EMBED_MAX_RETRIES)
    return None


def embed_text(text: str) -> tuple[list[float] | None, str]:
    """单条向量化，带内存缓存。

    返回 (向量, model名)。失败（未配置/API不通）返回 (None, model)，
    调用方据此降级。
    """
    cfg = _embedding_config()
    model = cfg["model"]
    if not cfg["base_url"] or not cfg["api_key"]:
        logger.warning("llm.yaml 未配置 embedding_base_url/api_key，跳过向量化")
        return None, model
    dimensions = int(cfg["dimensions"])
    return _embed_text_cached(text[:MAX_TEXT_LEN], model, dimensions)


@lru_cache(maxsize=1024)
def _embed_text_cached(
    text: str, model: str, dimensions: int
) -> tuple[list[float] | None, str]:
    """内部缓存层 — key 含 model/dimensions，配置变更自动失效旧条目。"""
    cfg = _embedding_config()
    vecs = _embed_batch_raw(
        [text], model, cfg["base_url"], cfg["api_key"], dimensions
    )
    return (vecs[0] if vecs else None), model


def embed_batch(texts: list[str]) -> tuple[list[list[float] | None], str]:
    """批量向量化（建索引用）。返回 (向量列表含None, model)。

    单批 EMBED_BATCH_SIZE 条，失败批次填 None。
    """
    cfg = _embedding_config()
    base_url = cfg["base_url"]
    api_key = cfg["api_key"]
    model = cfg["model"]
    if not base_url or not api_key:
        logger.warning("llm.yaml 未配置 embedding_base_url/api_key，跳过批量向量化")
        return [None] * len(texts), model
    dimensions = int(cfg["dimensions"])
    results: list[list[float] | None] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        chunk = [t[:MAX_TEXT_LEN] for t in texts[i : i + EMBED_BATCH_SIZE]]
        vecs = _embed_batch_raw(chunk, model, base_url, api_key, dimensions)
        if vecs:
            results.extend(vecs)
        else:
            results.extend([None] * len(chunk))
    return results, model


def clear_embed_cache() -> None:
    """清 _embed_text_cached 的 lru_cache（测试 / 换配置后用）。"""
    _embed_text_cached.cache_clear()


# ── 相似度 ────────────────────────────────────────────────────────


def _l2_norm(vec: list[float]) -> float:
    """L2 范数。"""
    return math.sqrt(sum(x * x for x in vec))


def cosine_sim(
    query_vec: list[float],
    stored_vec: list[float],
    stored_norm: float | None = None,
) -> float:
    """余弦相似度。stored_norm 预计算时省一次 norm 运算。"""
    dot = 0.0
    for a, b in zip(query_vec, stored_vec):
        dot += a * b
    qn = _l2_norm(query_vec)
    sn = stored_norm if stored_norm is not None else _l2_norm(stored_vec)
    return dot / (qn * sn) if qn and sn else 0.0


# ── RRF 融合 ──────────────────────────────────────────────────────


def hybrid_rank(
    sql_rows: list[dict],
    vec_rows: list[dict],
    keyword: str,
    id_col: str,
    limit: int,
    title_col: str = "title",
) -> list[dict]:
    """两路结果 RRF 融合排序。

    - 各路按已有顺序转 rank dict {id: rank_index}（0 = 最相关）
    - RRF score = Σ 1/(RRF_K + rank + 1)，两路都命中的得分最高
    - 精确命中（keyword == title/name）置顶，不被向量排下去
    - vec_rows 为空 → 直接返回 sql_rows（降级）

    Args:
        sql_rows: SQL LIKE 路的结果（已按 _score 排序）
        vec_rows: 向量路的结果（已按 cosine 排序）
        keyword: 查询关键词（精确命中判断用）
        id_col: 唯一标识列名（bug_id / rowid）
        limit: 返回条数
        title_col: 标题列名（精确命中判断用，bug=title, task=name）
    """
    if not vec_rows:
        return sql_rows[:limit]

    sql_rank = {}
    for i, r in enumerate(sql_rows):
        rid = r.get(id_col)
        if rid is not None:
            sql_rank[str(rid)] = i

    vec_rank = {}
    for i, r in enumerate(vec_rows):
        rid = r.get(id_col)
        if rid is not None:
            vec_rank[str(rid)] = i

    # RRF 融合
    all_ids = set(sql_rank) | set(vec_rank)
    row_map = {}
    for r in sql_rows + vec_rows:
        rid = r.get(id_col)
        if rid is not None and str(rid) not in row_map:
            row_map[str(rid)] = r

    scored: list[tuple[float, dict]] = []
    for rid in all_ids:
        rrf = 0.0
        if rid in sql_rank:
            rrf += 1.0 / (RRF_K + sql_rank[rid] + 1)
        if rid in vec_rank:
            rrf += 1.0 / (RRF_K + vec_rank[rid] + 1)
        scored.append((rrf, row_map[rid]))

    scored.sort(key=lambda x: -x[0])

    # 精确命中置顶（标题/名称完全匹配的条目不可被向量排下去）
    if keyword:
        kw = keyword.lower().strip()
        if kw:
            exact = [r for _, r in scored if (r.get(title_col) or "").lower() == kw]
            rest = [r for _, r in scored if (r.get(title_col) or "").lower() != kw]
            return (exact + rest)[:limit]

    return [r for _, r in scored][:limit]


# ── 通用建索引 ────────────────────────────────────────────────────

CREATE_EMB_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS {table} (
    source_id  INTEGER PRIMARY KEY,
    embedding  TEXT NOT NULL,
    norm       REAL NOT NULL,
    model      TEXT NOT NULL,
    updated_at REAL NOT NULL
);
"""


def _get_dimensions_from_config() -> int:
    """从配置读 dimensions，默认 512。"""
    return int(_embedding_config()["dimensions"])


def build_embeddings(
    db_path: Path,
    emb_table: str,
    source_table: str,
    id_col: str,
    text_cols: list[str],
    model: str = "",
) -> str:
    """通用：读 source_table → 拼 text_cols → embed_batch → 写 emb_table。

    流式分批处理（每批 EMBED_BATCH_SIZE 条），每 1000 条 commit + 打印进度，
    避免一次性加载全部文本到内存，崩溃也不丢已完成批次。

    返回 "✅ X/Y 条 (model=...)" 或 "⚠️ ..."（API 不通 / 未配置）。
    """
    if not db_path.exists():
        return f"⚠️ 数据库不存在: {db_path}"

    cfg = _embedding_config()
    base_url = cfg["base_url"]
    api_key = cfg["api_key"]
    model = model or cfg["model"]
    dimensions = int(cfg["dimensions"])
    if not base_url or not api_key:
        return "⚠️ llm.yaml 未配置 embedding_base_url/api_key，无法向量化"

    cols_sql = ", ".join([id_col] + text_cols)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(CREATE_EMB_TABLE_SQL.format(table=emb_table))

        total = conn.execute(
            f"SELECT COUNT(*) FROM {source_table}"
        ).fetchone()[0]
        if total == 0:
            return f"⚠️ {source_table} 表为空，无可向量化的记录"

        conn.execute(f"DELETE FROM {emb_table}")
        conn.commit()

        n_indexed = 0
        n_failed = 0
        batch_start = 0
        cursor = conn.execute(f"SELECT {cols_sql} FROM {source_table}")
        progress_interval = 1000  # 每 1000 条打印一次进度

        while True:
            batch_rows = cursor.fetchmany(EMBED_BATCH_SIZE)
            if not batch_rows:
                break

            # 拼接文本
            batch_texts = []
            for row in batch_rows:
                parts = [(str(row[i + 1]) or "").strip() for i in range(len(text_cols))]
                text = " ".join(p for p in parts if p)[:MAX_TEXT_LEN]
                batch_texts.append(text)

            vecs = _embed_batch_raw(
                batch_texts, model, base_url, api_key, dimensions
            )
            used_model = model

            now = time.time()
            if vecs:
                for row, vec in zip(batch_rows, vecs):
                    if vec:
                        norm = _l2_norm(vec)
                        conn.execute(
                            f"INSERT OR REPLACE INTO {emb_table} VALUES (?,?,?,?,?)",
                            (row[0], json.dumps(vec), norm, used_model, now),
                        )
                        n_indexed += 1
                    else:
                        n_failed += 1
            else:
                # 整批失败（API 不通），填失败计数
                n_failed += len(batch_rows)

            batch_start += len(batch_rows)
            # 批次间短暂延迟，避免触发 API RPM 限流
            time.sleep(0.3)
            if batch_start % progress_interval < EMBED_BATCH_SIZE:
                pct = n_indexed * 100 // total
                print(
                    f"  进度: {batch_start:,}/{total:,} ({pct}%) "
                    f"已索引={n_indexed:,} 失败={n_failed:,}",
                    flush=True,
                )
                conn.commit()

        conn.commit()
    finally:
        conn.close()

    if n_indexed == 0:
        return "⚠️ 向量化全部失败（embedding API 不通或返回空），请检查 llm.yaml 配置"
    return f"✅ 向量索引重建完成: {n_indexed}/{total} 条 (model={model}, dim={dimensions}, 失败={n_failed})"


# ── 通用向量检索 ──────────────────────────────────────────────────


def vector_search(
    db_path: Path,
    emb_table: str,
    source_table: str,
    query_vec: list[float],
    model: str,
    id_col: str,
    domain_col: str = "",
    domain_val: str = "",
    limit: int = 30,
) -> list[dict]:
    """全量扫 emb_table（同 model），算 cosine top-k。

    domain 过滤：若 domain_col/domain_val 非空，join source_table 按
    domain_col LIKE domain_val 二次过滤。

    返回 list[dict]（完整 source 行），按相似度降序。
    """
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        # 检查 emb_table 是否存在 + model 是否一致
        try:
            cur = conn.execute(
                f"SELECT source_id, embedding, norm FROM {emb_table} WHERE model = ?",
                (model,),
            )
        except sqlite3.OperationalError:
            # 表不存在
            return []

        sims: list[tuple[float, int]] = []
        for row in cur:
            try:
                stored_vec = json.loads(row["embedding"])
            except (json.JSONDecodeError, TypeError):
                continue
            sim = cosine_sim(query_vec, stored_vec, row["norm"])
            sims.append((sim, row["source_id"]))

        if not sims:
            return []

        sims.sort(key=lambda x: -x[0])

        # 取 top-k 的 source_id，join 取完整行
        top_ids = [sid for _, sid in sims[:limit]]
        if not top_ids:
            return []

        placeholders = ",".join("?" * len(top_ids))
        query = f"SELECT * FROM {source_table} WHERE {id_col} IN ({placeholders})"
        result_rows = [dict(r) for r in conn.execute(query, top_ids)]
        # 用 str 统一 key（source_id 存 INTEGER，source 表 id 可能是 TEXT）
        row_by_id = {str(r[id_col]): r for r in result_rows}

        # 按相似度排序 + domain 过滤
        out: list[dict] = []
        for sim, sid in sims:
            r = row_by_id.get(str(sid))
            if r is None:
                continue
            if domain_col and domain_val:
                dv = (r.get(domain_col) or "").lower()
                if domain_val.lower() not in dv:
                    continue
            out.append(r)
            if len(out) >= limit:
                break
        return out
    finally:
        conn.close()


# ── 统计 ──────────────────────────────────────────────────────────


def embedding_stats(db_path: Path, emb_table: str, total: int) -> str:
    """返回向量索引覆盖率统计行。表不存在返回 '未构建'。"""
    if not db_path.exists():
        return "未构建"
    conn = sqlite3.connect(str(db_path))
    try:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {emb_table}").fetchone()[0]
        except sqlite3.OperationalError:
            return "未构建"
        pct = f"{n * 100 // total}%" if total else "0%"
        model_row = conn.execute(
            f"SELECT DISTINCT model FROM {emb_table} LIMIT 1"
        ).fetchone()
        model_name = model_row[0] if model_row else "?"
        return f"已索引: {n:,}/{total:,} ({pct}), model={model_name}"
    finally:
        conn.close()
