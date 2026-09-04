#!/usr/bin/env python3
"""citfix 批量接口 CLI 壳（stub，业务未实现）。

Usage:
  python scripts/citfix_batch.py resolve --operator alice --bug-ids 97203,97210 --resolve-result resolved
  python scripts/citfix_batch.py resolve --request path/to/request.json
  python scripts/citfix_batch.py query --batch-id CIT-BATCH-20260901-001
  python scripts/citfix_batch.py retry --batch-id ... --bug-ids 97210
  python scripts/citfix_batch.py stats --batch-id ...

契约: contracts/citfix_batch_api.json
单 Bug: /citfix {bug_id}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from citfix.batch_api import (  # noqa: E402
    stub_batch_query,
    stub_batch_resolve,
    stub_batch_retry,
    stub_batch_stats,
    to_json,
)


def _load_request(args: argparse.Namespace) -> dict:
    if args.request:
        p = Path(args.request)
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SystemExit("request JSON must be an object")
        return data
    req: dict = {"schema_version": "1.0"}
    if args.batch_id:
        req["batch_id"] = args.batch_id
    if args.bug_ids:
        req["bug_ids"] = args.bug_ids
    if args.operator:
        req["operator"] = args.operator
    if args.resolve_result:
        req["resolve_result"] = args.resolve_result
    if args.comment:
        req["comment"] = args.comment
    return req


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="citfix_batch",
        description="citfix batch API stub (reserved; not implemented)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_resolve = sub.add_parser("resolve", help="citfix_batch_resolve stub")
    p_resolve.add_argument("--request", help="full request JSON file")
    p_resolve.add_argument("--batch-id", default="")
    p_resolve.add_argument("--bug-ids", default="", help="comma-separated ids")
    p_resolve.add_argument("--operator", default="")
    p_resolve.add_argument("--resolve-result", default="resolved")
    p_resolve.add_argument("--comment", default="")

    p_query = sub.add_parser("query", help="citfix_batch_query stub")
    p_query.add_argument("--batch-id", default="")
    p_query.add_argument("--date", default="")

    p_retry = sub.add_parser("retry", help="citfix_batch_retry stub")
    p_retry.add_argument("--batch-id", default="")
    p_retry.add_argument("--bug-ids", default="")

    p_stats = sub.add_parser("stats", help="citfix_batch_stats stub")
    p_stats.add_argument("--batch-id", default="")

    args = parser.parse_args()

    if args.cmd == "resolve":
        result = stub_batch_resolve(_load_request(args))
    elif args.cmd == "query":
        result = stub_batch_query(batch_id=args.batch_id, date=args.date)
    elif args.cmd == "retry":
        result = stub_batch_retry(batch_id=args.batch_id, bug_ids=args.bug_ids)
    else:
        result = stub_batch_stats(batch_id=args.batch_id)

    print(to_json(result))
    # Stub always exits 0 on valid parse so callers can inspect JSON;
    # INVALID_REQUEST also exits 0 with ok=false (shell reserved).
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
