"""Offline fixture loader for /citfix project 01/02 when ZenTao my_bugs is empty."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from citfix.project_match import DiscoverResult, discover_project_cit_bugs


def load_project_discover_fixture(fixture_dir: Path) -> dict[str, Any]:
    """Load ``fixtures/project_discover`` style directory.

    Required: ``fixture.json`` with keys ``zentao_user``, ``products``, ``my_bugs``.
    Optional split files: ``products.json``, ``my_bugs.json`` override lists in fixture.json.
    """
    root = Path(fixture_dir)
    main = root / "fixture.json"
    if not main.is_file():
        raise FileNotFoundError(f"Missing {main} (need fixture.json)")
    data = json.loads(main.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("fixture.json must be an object")

    products_path = root / "products.json"
    bugs_path = root / "my_bugs.json"
    if products_path.is_file():
        data["products"] = json.loads(products_path.read_text(encoding="utf-8"))
    if bugs_path.is_file():
        data["my_bugs"] = json.loads(bugs_path.read_text(encoding="utf-8"))

    user = str(data.get("zentao_user") or "").strip()
    products = list(data.get("products") or [])
    bugs = list(data.get("my_bugs") or [])
    if not user:
        raise ValueError("fixture.zentao_user is required")
    if not products:
        raise ValueError("fixture.products is empty")
    return {"zentao_user": user, "products": products, "my_bugs": bugs}


def make_discover_fn_from_fixture(fixture_dir: Path) -> Callable[..., DiscoverResult]:
    """Return a discover_fn compatible with ``run_batch_intake(discover_fn=...)``."""
    loaded = load_project_discover_fixture(fixture_dir)
    user = loaded["zentao_user"]
    products = loaded["products"]
    bugs = loaded["my_bugs"]

    def _discover(alias: str, *, repo_root: Path | None = None, **_kwargs) -> DiscoverResult:
        return discover_project_cit_bugs(
            alias,
            repo_root=repo_root or Path("."),
            my_bugs_fn=lambda: bugs,
            list_products_fn=lambda limit=100: products,
            configured_user_fn=lambda: user,
        )

    return _discover
