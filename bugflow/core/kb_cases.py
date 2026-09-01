# -*- coding: utf-8 -*-
"""修复案例知识库 — Hybrid 检索 + 质量门禁 + 多人共享。

查 ~/.bugfix-flow/kb_cases.sqlite 的 fix_cases 表。修复成功后由
skill_feedback 回流，case 先入 status='pending'，auto-review 通过后
升 'approved' 才可被 search 返回（agent 只参考已审核案例）。

多人维护模式:
  - 每人本地跑 → kb_cases.sqlite 积累 pending/approved case
  - 定期 export approved case → 汇总到内网公共服务器
  - 其他人 import → 去重入库，source='sync' 标记来源

检索策略: SQL LIKE 粗筛 + 向量语义重排 + RRF 融合（与 kb_aosp 对称）。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from .config import _config_dir, load_llm_config
from .kb_base import (
    KBConfig,
    hybrid_search as _hybrid_search,
    reindex_kb as _reindex_base,
    sql_like_search as _sql_like_search,
    vector_coverage_section as _vec_coverage,
)

logger = logging.getLogger("core.kb_cases")

def _kb_cases_path() -> Path:
    """案例库 SQLite 路径: ~/.bugfix-flow/kb_cases.sqlite。"""
    return _config_dir() / "kb_cases.sqlite"


_CFG = KBConfig(
    db_filename="kb_cases.sqlite",
    emb_table="cases_embeddings",
    source_table="fix_cases",
    id_col="id",
    domain_col="subsystem",
    title_col="title",
    order_by_col="created_at",
    or_fallback_cols=["title", "root_cause", "fix_summary"],
    score_cols=["title", "root_cause"],
    reindex_text_cols=["title", "root_cause", "fix_summary"],
    domain_exact_match=True,
)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS fix_cases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    bug_id      TEXT NOT NULL,
    title       TEXT NOT NULL,
    subsystem   TEXT NOT NULL,
    root_cause  TEXT NOT NULL,
    fix_summary TEXT NOT NULL,
    fix_files   TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    author      TEXT,
    review_note TEXT,
    reviewed_at REAL,
    reviewed_by TEXT,
    source      TEXT NOT NULL DEFAULT 'local',
    created_at  REAL NOT NULL,
    UNIQUE(bug_id, subsystem)
)
"""

# case 的合法状态
_STATUS_PENDING = "pending"
_STATUS_APPROVED = "approved"
_STATUS_REJECTED = "rejected"
_VALID_STATUSES = {_STATUS_PENDING, _STATUS_APPROVED, _STATUS_REJECTED}


def _init_db(conn: sqlite3.Connection) -> None:
    """幂等建表 + 索引（读取路径也会调用，确保新库可工作）。"""
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subsystem ON fix_cases(subsystem)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON fix_cases(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bug_id ON fix_cases(bug_id)")
    conn.commit()


# ── 写入 ──────────────────────────────────────────────────────────

def _get_author() -> str:
    """获取当前用户标识（git config user.name 优先，回退 OS 用户名）。"""
    try:
        result = subprocess.run(
            ["git", "config", "user.name"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def add_case(
    bug_id: str,
    title: str,
    subsystem: str,
    root_cause: str,
    fix_summary: str,
    fix_files: list[str] | None = None,
    author: str = "",
    source: str = "local",
) -> int | None:
    """插入一条 case（status=pending），同 bug+subsystem 已存在则更新。

    Returns:
        case id（新建或已存在的），None 表示失败（不抛异常）。
    """
    db_path = _kb_cases_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        conn = sqlite3.connect(str(db_path))
        _init_db(conn)

        fix_files_json = json.dumps(fix_files or [], ensure_ascii=False)
        author = author or _get_author()

        conn.execute(
            """
            INSERT INTO fix_cases
                (bug_id, title, subsystem, root_cause, fix_summary,
                 fix_files, status, author, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bug_id, subsystem) DO UPDATE SET
                title=excluded.title,
                root_cause=excluded.root_cause,
                fix_summary=excluded.fix_summary,
                fix_files=excluded.fix_files,
                author=excluded.author
            """,
            (
                str(bug_id), title, subsystem, root_cause, fix_summary,
                fix_files_json, _STATUS_PENDING, author, source, time.time(),
            ),
        )
        conn.commit()
        # ON CONFLICT DO UPDATE 时 lastrowid 返回 0，需显式查询
        row = conn.execute(
            "SELECT id FROM fix_cases WHERE bug_id=? AND subsystem=?",
            (str(bug_id), subsystem),
        ).fetchone()
        case_id = row[0] if row else None
        conn.close()

        logger.info(
            "case 已入库 (bug #%s, %s, status=pending, id=%s)",
            bug_id, subsystem, case_id,
        )
        return case_id
    except Exception as e:
        logger.warning("case 入库失败（不影响主流程）: %s", e)
        return None


# ── 审核 ──────────────────────────────────────────────────────────

def list_pending_cases(limit: int = 20) -> list[dict]:
    """列出 pending 状态的 case（供 reviewer 审核）。"""
    db_path = _kb_cases_path()
    if not db_path.exists():
        return []

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        _init_db(conn)
        cur = conn.execute(
            "SELECT * FROM fix_cases WHERE status = ? ORDER BY created_at DESC LIMIT ?",
            (_STATUS_PENDING, limit),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def review_case(
    case_id: int,
    status: str,
    review_note: str = "",
    reviewed_by: str = "auto-reviewer",
) -> bool:
    """更新 case 审核状态。status: approved / rejected。"""
    if status not in {_STATUS_APPROVED, _STATUS_REJECTED}:
        logger.warning("无效审核状态: %s（仅 approved/rejected）", status)
        return False

    db_path = _kb_cases_path()
    if not db_path.exists():
        return False

    try:
        conn = sqlite3.connect(str(db_path))
        _init_db(conn)
        conn.execute(
            """
            UPDATE fix_cases
            SET status=?, review_note=?, reviewed_at=?, reviewed_by=?
            WHERE id=?
            """,
            (status, review_note, time.time(), reviewed_by, case_id),
        )
        conn.commit()
        conn.close()
        logger.info("case #%s 审核结果: %s (%s)", case_id, status, review_note[:80])
        return True
    except Exception as e:
        logger.warning("case 审核更新失败: %s", e)
        return False


_REVIEW_SYSTEM_PROMPT = """你是修复案例质量审核员。评估一条 Android Bug 修复案例的质量。

判定标准:
- approved: root_cause 明确指出了问题根因（具体到模块/机制），fix_summary 清晰描述了修复方式（具体到改了什么）
- rejected: root_cause 模糊（如"代码问题""逻辑错误"无细节），或 fix_summary 空泛（如"修复了bug"无实质内容），或两者矛盾

只返回 JSON: {"verdict": "approved" 或 "rejected", "reason": "一句话理由"}"""


def _call_llm_review(case: dict) -> tuple[str, str]:
    """调用 LLM 审核单条 case，返回 (verdict, reason)。

    失败时回退 (approved, "auto-review skipped") — 宁可放过不可误杀。
    """
    try:
        from .model_detect import get_api_credentials, _post_chat
    except ImportError:
        logger.debug("model_detect 不可用，跳过自动审核")
        return _STATUS_APPROVED, "auto-review unavailable"

    try:
        base_url, api_key = get_api_credentials()
    except Exception as e:
        logger.debug("无法获取 API 凭据，跳过自动审核: %s", e)
        return _STATUS_APPROVED, "no api credentials"

    llm_cfg = load_llm_config()
    model = llm_cfg.get("review_model") or llm_cfg.get("model") or "deepseek-v4-flash"

    fix_files = json.loads(case.get("fix_files") or "[]")
    user_msg = (
        f"Bug #{case.get('bug_id', '?')} — {case.get('title', '')}\n"
        f"子系统: {case.get('subsystem', '')}\n"
        f"根因: {case.get('root_cause', '')}\n"
        f"修复: {case.get('fix_summary', '')}\n"
        f"改动文件: {', '.join(fix_files) if fix_files else '(无)'}"
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "max_tokens": 256,
    }

    try:
        result = _post_chat(api_key, base_url, payload, timeout=60)
    except Exception as e:
        logger.debug("LLM 审核请求异常: %s", e)
        return _STATUS_APPROVED, "review request failed"

    if not result.get("ok"):
        logger.debug("LLM 审核请求失败: %s", result.get("body", "")[:200])
        return _STATUS_APPROVED, "review api error"

    try:
        content = result["data"]["choices"][0]["message"]["content"]
        # 提取 JSON（兼容 markdown 代码块包裹）
        content = content.strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        verdict_data = json.loads(content)
        verdict = verdict_data.get("verdict", "").lower().strip()
        reason = verdict_data.get("reason", "")
        if verdict not in {_STATUS_APPROVED, _STATUS_REJECTED}:
            return _STATUS_APPROVED, "invalid verdict, defaulting to approved"
        return verdict, reason
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        logger.debug("LLM 审核结果解析失败: %s", e)
        return _STATUS_APPROVED, "review parse failed"


def review_pending_cases(limit: int = 20) -> str:
    """自动审核 pending case（LLM 评估根因/修复质量）。

    逐条调 LLM，通过→approved，不通过→rejected。失败默认 approved（不阻塞）。
    """
    pending = list_pending_cases(limit)
    if not pending:
        return "✅ 没有 pending case 需要审核。"

    approved = 0
    rejected = 0
    for case in pending:
        verdict, reason = _call_llm_review(case)
        ok = review_case(
            case["id"], verdict, reason, reviewed_by="auto-reviewer",
        )
        if ok and verdict == _STATUS_APPROVED:
            approved += 1
        elif ok and verdict == _STATUS_REJECTED:
            rejected += 1

    return (
        f"📋 自动审核完成: {len(pending)} 条 pending → "
        f"{approved} approved, {rejected} rejected"
    )


def review_single_case(case_id: int) -> str:
    """审核单条 case（LLM 评估）。已审核则跳过。

    供 kb_add_case 工具在写入后立即审核刚入库的 case。
    """
    db_path = _kb_cases_path()
    if not db_path.exists():
        return "❌ 案例库不存在。"

    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        _init_db(conn)
        row = conn.execute(
            "SELECT * FROM fix_cases WHERE id=?", (case_id,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return f"❌ 未找到 case id={case_id}。"

    case = dict(row)
    current_status = case.get("status", "")
    if current_status != _STATUS_PENDING:
        return f"ℹ️ case #{case_id} 当前状态为 {current_status}，无需审核。"

    verdict, reason = _call_llm_review(case)
    review_case(case_id, verdict, reason, reviewed_by="auto-reviewer")
    return f"✅ case #{case_id} 审核结果: {verdict} ({reason})"


# ── 检索 ──────────────────────────────────────────────────────────

def _search_kb_cases_sql(
    keyword: str = "",
    subsystem: str = "",
    status: str = _STATUS_APPROVED,
    limit: int = 10,
) -> list[dict]:
    """SQL LIKE 搜索案例库，默认只搜 approved。

    多词: 先 AND 匹配 title，无结果再 OR 回退到 title/root_cause/fix_summary。
    subsystem 过滤: 非空时精确匹配 subsystem 字段。
    """
    keyword = (keyword or "").strip()
    subsystem = (subsystem or "").strip()
    status = status if status in _VALID_STATUSES else _STATUS_APPROVED

    if not keyword and not subsystem:
        return []

    return _sql_like_search(
        _CFG, keyword, subsystem, limit,
        init_db_fn=_init_db,
        extra_where_sql="status = ?",
        extra_where_params=[status],
        db_path=_kb_cases_path(),
    )


def _search_kb_cases(
    keyword: str = "",
    subsystem: str = "",
    status: str = _STATUS_APPROVED,
    limit: int = 10,
) -> list[dict]:
    """Hybrid 检索: SQL LIKE 粗筛 + 向量语义重排 + RRF 融合。"""
    keyword = (keyword or "").strip()
    subsystem = (subsystem or "").strip()
    status = status if status in _VALID_STATUSES else _STATUS_APPROVED

    if not keyword and not subsystem:
        return []

    return _hybrid_search(
        _CFG, keyword, subsystem, limit,
        init_db_fn=_init_db,
        extra_where_sql="status = ?",
        extra_where_params=[status],
        vec_filter_col="status",
        vec_filter_val=status,
        db_path=_kb_cases_path(),
    )


def search_kb_cases_formatted(
    keyword: str = "",
    subsystem: str = "",
    limit: int = 10,
) -> str:
    """搜索已审核案例并格式化输出。供 agent 分析前参考历史修复经验。"""
    db_path = _kb_cases_path()
    if not db_path.exists():
        return (
            "ℹ️ 案例库尚未建立。修复成功提交后会自动积累历史案例。\n"
            "首次使用无历史案例可参考，直接进入源码分析。"
        )

    if not keyword and not subsystem:
        return "❌ 请提供 keyword（关键词）或 domain/subsystem（子系统）。"

    rows = _search_kb_cases(keyword, subsystem, _STATUS_APPROVED, limit)
    if not rows:
        return (
            f"📭 案例库中未找到匹配「{keyword or subsystem}」的已审核案例。\n"
            "建议: 尝试更通用的关键词，或查 Bug 知识库 / AOSP 知识库。"
        )

    lines = [f"🔍 案例库搜索「{keyword or subsystem}」找到 {len(rows)} 条已审核案例：\n"]
    for i, r in enumerate(rows, 1):
        title = r.get("title") or ""
        if len(title) > 100:
            title = title[:97] + "..."

        sub = r.get("subsystem") or "?"
        author = r.get("author") or "?"
        bug_id = r.get("bug_id") or "?"

        lines.append(f"{i}. 📋 [Bug #{bug_id}] {title}")
        lines.append(f"   子系统: {sub} | 修复者: {author}")

        rc = r.get("root_cause") or ""
        if rc:
            lines.append(f"   🔍 根因: {rc}")

        fs = r.get("fix_summary") or ""
        if fs:
            lines.append(f"   🔧 修复: {fs}")

        fix_files = json.loads(r.get("fix_files") or "[]")
        if fix_files:
            lines.append(f"   📁 文件: {', '.join(fix_files[:3])}")

    lines.append(
        "\n💡 以上为已审核的历史修复案例，参考其根因分析路径辅助定位当前问题。"
    )
    return "\n".join(lines)


# ── 统计 ──────────────────────────────────────────────────────────

def kb_cases_stats() -> str:
    """案例库统计: 总量 / 状态分布 / 子系统分布 / 审核进度。"""
    db_path = _kb_cases_path()
    if not db_path.exists():
        return "ℹ️ 案例库尚未建立（kb_cases.sqlite 不存在）。"

    conn = sqlite3.connect(str(db_path))
    _init_db(conn)
    total = conn.execute("SELECT COUNT(*) FROM fix_cases").fetchone()[0]

    status_dist = conn.execute(
        "SELECT status, COUNT(*) as cnt FROM fix_cases GROUP BY status ORDER BY cnt DESC"
    ).fetchall()

    subsystem_dist = conn.execute(
        "SELECT subsystem, COUNT(*) as cnt FROM fix_cases "
        "GROUP BY subsystem ORDER BY cnt DESC"
    ).fetchall()

    author_dist = conn.execute(
        "SELECT author, COUNT(*) as cnt FROM fix_cases "
        "WHERE author IS NOT NULL AND author != '' "
        "GROUP BY author ORDER BY cnt DESC"
    ).fetchall()

    conn.close()

    lines = [f"# 案例库统计\n", f"- 总记录: {total:,} 条\n"]

    if total > 0:
        lines.append("\n## 审核状态分布")
        for s, n in status_dist:
            icon = {"approved": "✅", "pending": "⏳", "rejected": "❌"}.get(s, "❓")
            lines.append(f"- {icon} {s}: {n:,}")

        pending_count = sum(n for s, n in status_dist if s == _STATUS_PENDING)
        if pending_count > 0:
            lines.append(
                f"\n⏳ {pending_count} 条 pending 待审核 — "
                "运行 kb(action='review', kb_type='cases') 自动审核"
            )

        lines.append("\n## 子系统分布")
        for sub, n in subsystem_dist:
            lines.append(f"- {sub}: {n:,}")

        if author_dist:
            lines.append("\n## 修复者分布")
            for a, n in author_dist:
                lines.append(f"- {a}: {n:,}")

    lines.append(_vec_coverage(_CFG, db_path, total, "kb(action='reindex', kb_type='cases')"))

    return "\n".join(lines)


# ── 向量索引重建 ──────────────────────────────────────────────────

def reindex_kb_cases(model: str = "") -> str:
    """重建案例库向量索引。从 fix_cases 表读 title+root_cause+fix_summary 向量化。"""
    return _reindex_base(_CFG, model, db_path=_kb_cases_path())


# ── 多人共享: 导出/导入 ────────────────────────────────────────────

def export_cases(
    path: str = "",
    status: str = _STATUS_APPROVED,
) -> str:
    """导出 case 到 JSONL 文件，供汇总到内网公共服务器。

    默认只导出 approved case（经过审核的高质量案例）。
    每行一条 JSON: {bug_id, title, subsystem, root_cause, fix_summary, fix_files, author, source}
    """
    db_path = _kb_cases_path()
    if not db_path.exists():
        return "❌ 案例库不存在，无可导出内容。"

    if not path:
        path = str(_config_dir() / "cases_export.jsonl")

    status = status if status in _VALID_STATUSES else _STATUS_APPROVED

    conn = sqlite3.connect(str(db_path))
    _init_db(conn)
    cur = conn.execute(
        "SELECT bug_id, title, subsystem, root_cause, fix_summary, fix_files, "
        "author, source FROM fix_cases WHERE status = ? ORDER BY created_at",
        (status,),
    )
    rows = cur.fetchall()
    conn.close()

    export_path = Path(path)
    export_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with export_path.open("w", encoding="utf-8") as f:
        for row in rows:
            case = {
                "bug_id": row[0],
                "title": row[1],
                "subsystem": row[2],
                "root_cause": row[3],
                "fix_summary": row[4],
                "fix_files": json.loads(row[5] or "[]"),
                "author": row[6] or "",
                "source": "sync",  # 导出的 case 在导入端标记为 sync
            }
            f.write(json.dumps(case, ensure_ascii=False) + "\n")
            count += 1

    return f"✅ 已导出 {count} 条 {status} case 到 {export_path}"


def import_cases(path: str) -> str:
    """从 JSONL 文件导入 case（汇总其他人的案例到本地库）。

    导入的 case 直接标记 status=approved, source=sync（已在他端审核过）。
    同 bug_id+subsystem 的 case 通过 UPSERT 去重，保留本地已有内容。
    """
    import_path = Path(path)
    if not import_path.is_file():
        return f"❌ 文件不存在: {path}"

    db_path = _kb_cases_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    imported = 0
    skipped = 0
    conn = sqlite3.connect(str(db_path))
    try:
        _init_db(conn)

        with import_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    case = json.loads(line)
                except json.JSONDecodeError:
                    skipped += 1
                    continue

                bug_id = str(case.get("bug_id", "")).strip()
                subsystem = case.get("subsystem", "").strip()
                root_cause = case.get("root_cause", "").strip()
                fix_summary = case.get("fix_summary", "").strip()

                if not bug_id or not root_cause or not fix_summary:
                    skipped += 1
                    continue

                fix_files_json = json.dumps(case.get("fix_files") or [], ensure_ascii=False)
                author = case.get("author") or "sync"

                # 检查是否已存在（同 bug+subsystem）
                existing = conn.execute(
                    "SELECT id, source FROM fix_cases WHERE bug_id=? AND subsystem=?",
                    (bug_id, subsystem),
                ).fetchone()

                if existing:
                    # 已存在则跳过（不覆盖本地 case）
                    skipped += 1
                    continue

                conn.execute(
                    """
                    INSERT INTO fix_cases
                        (bug_id, title, subsystem, root_cause, fix_summary,
                         fix_files, status, author, source, created_at,
                         reviewed_at, reviewed_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        bug_id, case.get("title", ""), subsystem,
                        root_cause, fix_summary, fix_files_json,
                        _STATUS_APPROVED, author, "sync", time.time(),
                        time.time(), "imported",
                    ),
                )
                imported += 1

        conn.commit()
    finally:
        conn.close()

    return f"✅ 导入完成: {imported} 条新增, {skipped} 条跳过（重复/无效）"
