"""Project alias matching and CIT bug discovery for /citfix project."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from citfix.human_cases import resolve_verification
from citfix.stages.prepare_common import _cit_title_ok


@dataclass
class ProductHit:
    product_id: str
    name: str
    code: str = ""
    score: int = 0


@dataclass
class CandidateBug:
    bug_id: str
    title: str
    status: str
    product_id: str
    product_name: str
    assigned_to: str
    severity: str = ""
    steps: str = ""
    cit_ok: bool = False
    verify_mode: str = "auto"
    reject_reason: str = ""


@dataclass
class DiscoverResult:
    alias: str
    zentao_user: str
    matched_product: ProductHit | None
    candidates: list[CandidateBug] = field(default_factory=list)
    accepted: list[CandidateBug] = field(default_factory=list)
    rejected: list[CandidateBug] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _compact(s: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", _norm(s))


def score_product_alias(alias: str, name: str, code: str = "") -> int:
    """Higher is better. 0 = no match.

    Allows keyword match: alias ``slb783`` hits ``SLB783 - Android14``.
    """
    a = _norm(alias)
    if not a:
        return 0
    n = _norm(name)
    c = _norm(code)
    ac = _compact(alias)
    nc = _compact(name)
    cc = _compact(code)

    if a == n or (c and a == c):
        return 100
    if ac and (ac == nc or (cc and ac == cc)):
        return 95
    # token equality (split on spaces / dashes)
    name_tokens = [t for t in re.split(r"[\s\-_+/]+", n) if t]
    code_tokens = [t for t in re.split(r"[\s\-_+/]+", c) if t]
    if a in name_tokens or a in code_tokens:
        return 90
    if ac and ac in name_tokens:
        return 88
    # substring keyword (alias inside product name)
    if len(a) >= 3 and (a in n or (c and a in c)):
        return 70
    if len(ac) >= 3 and (ac in nc or (cc and ac in cc)):
        return 65
    return 0


def match_products(alias: str, products: list[dict[str, Any]]) -> list[ProductHit]:
    hits: list[ProductHit] = []
    for p in products:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "")
        code = str(p.get("code") or "")
        pid = str(p.get("id") or "")
        sc = score_product_alias(alias, name, code)
        if sc > 0:
            hits.append(ProductHit(product_id=pid, name=name, code=code, score=sc))
    hits.sort(key=lambda h: (-h.score, h.name.lower()))
    return hits


def pick_best_product(alias: str, products: list[dict[str, Any]]) -> ProductHit | None:
    hits = match_products(alias, products)
    if not hits:
        return None
    best = hits[0]
    # Ambiguous if another hit shares same top score band and different id
    rivals = [h for h in hits[1:] if h.score >= best.score - 5 and h.product_id != best.product_id]
    if rivals and best.score < 90:
        return None  # caller should report ambiguity
    if rivals and best.score >= 90:
        # still prefer exact/token; only ambiguous if multiple exact
        exact = [h for h in hits if h.score >= 90]
        if len(exact) > 1 and len({h.product_id for h in exact}) > 1:
            return None
    return best


def _assignee_matches(assigned: str, user: str) -> bool:
    a = _norm(assigned)
    u = _norm(user)
    if not u:
        return False
    if not a:
        return False
    if a == u:
        return True
    # ZenTao sometimes returns "realname(account)" or account only
    if u in a or a in u:
        return True
    return False


def _plan_bank_products(repo_root: Path) -> list[dict[str, Any]]:
    root = repo_root / "plan_bank"
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for d in root.iterdir():
        if d.is_dir() and not d.name.startswith("."):
            out.append({"id": d.name, "name": d.name, "code": ""})
    return out


def discover_project_cit_bugs(
    alias: str,
    *,
    repo_root: Path,
    my_bugs_fn=None,
    list_products_fn=None,
    configured_user_fn=None,
    ensure_product_name_fn=None,
) -> DiscoverResult:
    """Pull current user's bugs, filter by product alias + CIT + verify_mode=auto."""
    if my_bugs_fn and list_products_fn and configured_user_fn:
        user_fn = configured_user_fn
        products_fn = list_products_fn
        bugs_fn = my_bugs_fn
    else:
        from bugflow.core import zentao

        user_fn = configured_user_fn or zentao.configured_user
        products_fn = list_products_fn or zentao.list_products
        bugs_fn = my_bugs_fn or zentao.my_bugs_all
    ensure_name = ensure_product_name_fn

    result = DiscoverResult(alias=alias, zentao_user="", matched_product=None)
    try:
        user = str(user_fn() or "").strip()
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"zentao user unavailable: {e}")
        return result
    result.zentao_user = user
    if not user:
        result.errors.append(
            "zentao.yaml has empty user — configure ZenTao MCP (~/.bugfix-flow/zentao.yaml)"
        )
        return result

    try:
        products = list(products_fn(limit=200) or [])
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"list_products failed: {e}")
        products = []

    # Fallbacks: plan_bank dirs + products inferred from my_bugs (after fetch)
    products.extend(_plan_bank_products(repo_root))

    try:
        raw_bugs = list(bugs_fn() or [])
    except Exception as e:  # noqa: BLE001
        result.errors.append(f"my_bugs failed: {e}")
        return result

    for bug in raw_bugs:
        if not isinstance(bug, dict):
            continue
        if ensure_name:
            try:
                ensure_name(bug)
            except Exception:
                pass
        pname = str(bug.get("product_name") or "")
        pid = str(bug.get("product") or "")
        if pname or pid:
            products.append({"id": pid or pname, "name": pname or pid, "code": ""})

    # de-dupe products by name+id
    seen: set[str] = set()
    uniq: list[dict[str, Any]] = []
    for p in products:
        key = f"{p.get('id')}|{p.get('name')}"
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    products = uniq

    hits = match_products(alias, products)
    best = pick_best_product(alias, products)
    if best is None:
        if hits:
            names = ", ".join(f"{h.name}(#{h.product_id})" for h in hits[:5])
            result.errors.append(
                f"Ambiguous project alias {alias!r}; candidates: {names}. Use a sharper alias."
            )
        else:
            result.errors.append(
                f"No ZenTao product matched alias {alias!r}. "
                "Try a keyword from the product name (e.g. slb783)."
            )
        return result
    result.matched_product = best

    for bug in raw_bugs:
        if not isinstance(bug, dict):
            continue
        bid = str(bug.get("id") or bug.get("bug_id") or "").strip()
        title = str(bug.get("title") or "")
        steps = str(bug.get("steps") or "")
        status = str(bug.get("status") or "")
        assigned = str(bug.get("assignedTo") or bug.get("assigned_to") or "")
        product_field = str(bug.get("product_name") or bug.get("product") or "")
        cand = CandidateBug(
            bug_id=bid,
            title=title,
            status=status,
            product_id=str(bug.get("product") or ""),
            product_name=str(bug.get("product_name") or product_field),
            assigned_to=assigned,
            severity=str(bug.get("severity") or ""),
            steps=steps,
            cit_ok=_cit_title_ok(title, steps),
        )
        result.candidates.append(cand)

        if not bid:
            cand.reject_reason = "missing_bug_id"
            result.rejected.append(cand)
            continue
        if not _assignee_matches(assigned, user):
            cand.reject_reason = f"assignedTo={assigned!r} != current user {user!r}"
            result.rejected.append(cand)
            continue
        # product match: id equality or fuzzy match on the bug's product name only
        bug_prod_name = cand.product_name or product_field
        prod_ok = (
            str(bug.get("product") or "") == str(best.product_id)
            or _norm(bug_prod_name) == _norm(best.name)
            or score_product_alias(alias, bug_prod_name, "") > 0
        )
        if not prod_ok:
            cand.reject_reason = f"product mismatch ({product_field})"
            result.rejected.append(cand)
            continue
        if not cand.cit_ok:
            cand.reject_reason = "cit_title_gate"
            result.rejected.append(cand)
            continue

        ver = resolve_verification(
            repo_root,
            best.name or cand.product_name or "DEFAULT",
            title,
            steps,
        )
        mode = str(ver.get("mode") or "auto").lower()
        cand.verify_mode = mode
        if mode != "auto":
            cand.reject_reason = f"verify_mode={mode} (project batch requires auto)"
            result.rejected.append(cand)
            continue

        result.accepted.append(cand)

    # stable order by bug_id
    result.accepted.sort(key=lambda c: int(c.bug_id) if c.bug_id.isdigit() else c.bug_id)
    return result
