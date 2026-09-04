"""citfix-mcp — CIT 批量接口壳（stub，业务未实现）。

工具（均 reserved / NOT_IMPLEMENTED）：
  citfix_batch_resolve — 批量解决写回
  citfix_batch_query   — 查批次
  citfix_batch_retry   — 失败重试
  citfix_batch_stats   — 结果统计

契约：contracts/citfix_batch_api.json
说明：docs/citfix-batch-api.md

单 Bug 请继续使用 /citfix {bug_id}。
"""

from __future__ import annotations

import json
import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.dirname(_here)
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("ERROR: mcp SDK 未安装。请 pip install -r requirements-mcp.txt", file=sys.stderr)
    sys.exit(1)

from citfix.batch_api import (
    stub_batch_query,
    stub_batch_resolve,
    stub_batch_retry,
    stub_batch_stats,
    to_json,
)

mcp = FastMCP("citfix-mcp")


@mcp.tool()
def citfix_batch_resolve(
    bug_ids: str = "",
    operator: str = "",
    resolve_result: str = "resolved",
    comment: str = "",
    batch_id: str = "",
    request_json: str = "",
) -> str:
    """【预留壳】批量解决 Bug 写回（不执行业务）。

    与 bugfix 批量语义对齐：batch_id / bug_ids / operator / resolve_result / comment。
    当前固定返回 status=reserved、error_code=NOT_IMPLEMENTED。
    单 Bug 请用 /citfix {bug_id}。

    Args:
        bug_ids: 逗号分隔 ID，如 "97203,97210"；或配合 request_json
        operator: 操作人（必填）
        resolve_result: resolved|active|closed|skip|manual
        comment: 解决备注
        batch_id: 可选批次 ID
        request_json: 完整请求 JSON（优先；字段见 citfix_batch_api.json）
    """
    req: dict = {}
    if request_json and request_json.strip():
        try:
            req = json.loads(request_json)
            if not isinstance(req, dict):
                return to_json(
                    {
                        "ok": False,
                        "error_code": "INVALID_REQUEST",
                        "errors": [
                            {
                                "error_code": "INVALID_REQUEST",
                                "message": "request_json must be an object",
                            }
                        ],
                    }
                )
        except json.JSONDecodeError as e:
            return to_json(
                {
                    "ok": False,
                    "error_code": "INVALID_REQUEST",
                    "errors": [
                        {
                            "error_code": "INVALID_REQUEST",
                            "message": f"request_json parse error: {e}",
                        }
                    ],
                }
            )
    if bug_ids and "bug_ids" not in req:
        req["bug_ids"] = bug_ids
    if operator and "operator" not in req:
        req["operator"] = operator
    if resolve_result and "resolve_result" not in req:
        req["resolve_result"] = resolve_result
    if comment and "comment" not in req:
        req["comment"] = comment
    if batch_id and "batch_id" not in req:
        req["batch_id"] = batch_id
    if "schema_version" not in req:
        req["schema_version"] = "1.0"
    return to_json(stub_batch_resolve(req))


@mcp.tool()
def citfix_batch_query(batch_id: str = "", date: str = "") -> str:
    """【预留壳】查询批次状态（未实现）。"""
    return to_json(stub_batch_query(batch_id=batch_id, date=date))


@mcp.tool()
def citfix_batch_retry(batch_id: str = "", bug_ids: str = "") -> str:
    """【预留壳】对批次失败项重试（未实现）。"""
    return to_json(stub_batch_retry(batch_id=batch_id, bug_ids=bug_ids))


@mcp.tool()
def citfix_batch_stats(batch_id: str = "") -> str:
    """【预留壳】批次结果统计（未实现）。"""
    return to_json(stub_batch_stats(batch_id=batch_id))


if __name__ == "__main__":
    mcp.run(transport="stdio")
