"""Parse /citfix slash commands and CLI argv (bug id vs project alias vs diff)."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Sequence


@dataclass(frozen=True)
class CitfixCommand:
    """Normalized citfix invocation."""

    kind: str  # "bug" | "bugs" | "project" | "diff"
    bug_id: str | None = None
    bug_ids: tuple[str, ...] = field(default_factory=tuple)
    project_alias: str | None = None
    device_serial: str | None = None
    resume: bool = False
    status: bool = False
    new: bool = False
    run_id: str | None = None
    dry_run: bool = False
    debug: bool = False
    fixture: str | None = None
    claim_only: bool = False
    as_json: bool = False
    raw: str = ""


_PROJECT_RE = re.compile(r"^project$", re.IGNORECASE)
_DIFF_RE = re.compile(r"^(diff|show-diff|show_diff)$", re.IGNORECASE)
_BUG_ID_RE = re.compile(r"^\d+$")


def _flag_value(tokens: list[str], idx: int) -> tuple[str | None, int]:
    """Return value for --flag [value]; advances idx past value when present."""
    if idx + 1 >= len(tokens):
        return None, idx
    nxt = tokens[idx + 1]
    if nxt.startswith("-"):
        return None, idx
    return nxt, idx + 1


def parse_citfix_tokens(tokens: Sequence[str], *, raw: str = "") -> CitfixCommand:
    """Parse tokens after optional leading '/citfix' or 'citfix'."""
    parts = [t for t in tokens if t]
    if parts and parts[0].lower().lstrip("/") in ("citfix",):
        parts = parts[1:]

    resume = False
    status = False
    new = False
    dry_run = False
    debug = False
    claim_only = False
    as_json = False
    want_diff = False
    run_id: str | None = None
    device: str | None = None
    fixture: str | None = None
    positional: list[str] = []

    i = 0
    while i < len(parts):
        t = parts[i]
        low = t.lower()
        if low in ("--resume", "-r"):
            resume = True
        elif low == "--status":
            status = True
        elif low == "--new":
            new = True
        elif low in ("--dry-run", "--dry_run"):
            dry_run = True
        elif low in ("--debug", "-d"):
            debug = True
        elif low in ("--claim-only", "--claim_only"):
            claim_only = True
        elif low in ("--diff", "--show-diff"):
            want_diff = True
        elif low == "--json":
            as_json = True
        elif low == "--run-id":
            run_id, i = _flag_value(parts, i)
        elif low.startswith("--run-id="):
            run_id = t.split("=", 1)[1] or None
        elif low == "--device":
            device, i = _flag_value(parts, i)
        elif low.startswith("--device="):
            device = t.split("=", 1)[1] or None
        elif low == "--fixture":
            fixture, i = _flag_value(parts, i)
        elif low.startswith("--fixture="):
            fixture = t.split("=", 1)[1] or None
        elif t.startswith("-"):
            raise ValueError(f"Unknown flag: {t}")
        else:
            positional.append(t)
        i += 1

    if not positional:
        raise ValueError(
            "Usage: /citfix <bug_id> [moreIds…] | /citfix diff <ids> | "
            "/citfix project <alias>"
        )

    if _PROJECT_RE.match(positional[0]):
        if len(positional) < 2 or not str(positional[1]).strip():
            raise ValueError("Usage: /citfix project <alias> [--device SN]")
        alias = str(positional[1]).strip()
        if len(positional) > 2:
            raise ValueError(
                f"Unexpected args after project alias: {positional[2:]}. "
                "Use --device <SN> for device override."
            )
        return CitfixCommand(
            kind="project",
            project_alias=alias,
            device_serial=device,
            resume=resume,
            status=status,
            new=new,
            run_id=run_id,
            dry_run=dry_run,
            debug=debug,
            fixture=fixture,
            claim_only=claim_only,
            as_json=as_json,
            raw=raw or " ".join(tokens),
        )

    # /citfix diff 81097 81098  — ONLY when "diff" keyword (or --diff flag) present
    if _DIFF_RE.match(positional[0]):
        ids = positional[1:]
        if not ids or any(not _BUG_ID_RE.match(x) for x in ids):
            raise ValueError("Usage: /citfix diff <bug_id> [moreIds…]")
        return CitfixCommand(
            kind="diff",
            bug_id=ids[0],
            bug_ids=tuple(ids),
            as_json=as_json,
            debug=debug,
            raw=raw or " ".join(tokens),
        )

    # All positional digits → one or more bug pipeline runs (NOT diff)
    if all(_BUG_ID_RE.match(x) for x in positional):
        if want_diff:
            # --diff / --show-diff also contain the word "diff"
            return CitfixCommand(
                kind="diff",
                bug_id=positional[0],
                bug_ids=tuple(positional),
                as_json=as_json,
                debug=debug,
                raw=raw or " ".join(tokens),
            )
        if device:
            raise ValueError("--device is only valid with /citfix project <alias>")
        if fixture:
            raise ValueError("--fixture is only valid with /citfix project <alias>")
        if claim_only:
            raise ValueError("--claim-only is only valid with /citfix project <alias>")
        if len(positional) == 1:
            return CitfixCommand(
                kind="bug",
                bug_id=positional[0],
                bug_ids=(positional[0],),
                resume=resume,
                status=status,
                new=new,
                run_id=run_id,
                dry_run=dry_run,
                debug=debug,
                fixture=None,
                claim_only=False,
                as_json=as_json,
                raw=raw or " ".join(tokens),
            )
        return CitfixCommand(
            kind="bugs",
            bug_id=positional[0],
            bug_ids=tuple(positional),
            resume=resume,
            status=status,
            new=new,
            run_id=None,  # per-bug runs; --run-id only for single bug
            dry_run=dry_run,
            debug=debug,
            fixture=None,
            claim_only=False,
            as_json=as_json,
            raw=raw or " ".join(tokens),
        )

    raise ValueError(
        f"Expected bug id(s) or 'project <alias>' or 'diff <ids>', got {positional!r}"
    )


def parse_citfix_text(text: str) -> CitfixCommand:
    """Parse a user/agent slash line such as '/citfix Project SLB783 --device x'."""
    s = (text or "").strip()
    if not s:
        raise ValueError("Empty citfix command")
    try:
        tokens = shlex.split(s, posix=False)
    except ValueError:
        tokens = s.split()
    return parse_citfix_tokens(tokens, raw=s)
