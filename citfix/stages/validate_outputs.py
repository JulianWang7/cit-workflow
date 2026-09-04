"""Agent-stage output JSON validation (07–14)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from citfix.human_cases import VERIFY_MODES

def _assertion_map(data: dict[str, Any], key: str) -> dict[str, Any]:
    raw = data.get(key)
    if isinstance(raw, dict):
        return raw
    return {}


def _all_assertions_passed(assertions: dict[str, Any]) -> tuple[bool, list[str]]:
    errs: list[str] = []
    if not assertions:
        return False, ["auto_assertions empty"]
    for name, body in assertions.items():
        if isinstance(body, dict):
            passed = body.get("passed")
        else:
            passed = body
        if passed is not True:
            errs.append(f"auto_assertions.{name}.passed must be true (got {passed!r})")
    return (len(errs) == 0), errs


def _verify_mode_from_context(context: dict[str, Any] | None) -> str:
    if not context:
        return ""
    routing = context.get("routing") if isinstance(context.get("routing"), dict) else {}
    mode = str(routing.get("verify_mode") or "").strip().lower()
    if mode in VERIFY_MODES:
        return mode
    # Legacy fixture value "cit" is not a verification mode.
    return ""


def _validate_agent_output(
    sid: str,
    out_path: Path,
    *,
    context: dict[str, Any] | None = None,
    run_dir: Path | None = None,
    bug_id: str = "",
) -> list[str]:
    """Reject paper-complete JSON that would let the pipeline slide past build/deploy."""
    try:
        data = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"output is not valid JSON: {e}"]
    if not isinstance(data, dict):
        return ["output root must be a JSON object"]

    errors: list[str] = []
    verify_mode = _verify_mode_from_context(context)

    if sid == "07_analysis":
        if str(data.get("marker") or "") != "ROOT_CAUSE_FOUND":
            errors.append("marker must be ROOT_CAUSE_FOUND")
        if not str(data.get("root_cause") or "").strip():
            errors.append("root_cause required")
        ev = data.get("evidence_paths")
        if not isinstance(ev, list) or not ev:
            errors.append("evidence_paths must be a non-empty array")
        if not str(data.get("recommended_fix") or "").strip():
            errors.append("recommended_fix required")

    if sid == "08_changes":
        rs = str(data.get("review_status") or "").lower()
        if rs != "pass":
            errors.append(
                f"review_status must be 'pass' before 08 completes (got {rs!r}); "
                "run cit-review and rewrite change JSON"
            )
        if not data.get("files_changed"):
            errors.append("files_changed empty")
        if not str(data.get("source_base_sha") or "").strip():
            errors.append(
                "source_base_sha required — SSH: git -C $code_root rev-parse HEAD before/after edit"
            )
        # Prefer hash of unified diff; fallback allowed only if explicitly marked
        if not str(data.get("patch_sha256") or "").strip():
            errors.append(
                "patch_sha256 required — sha256 of `git diff` text (or of the patch file body)"
            )

    if sid == "09_jenkins_build":
        mode = str(data.get("build_mode") or "").strip().lower()
        if mode in ("", "skip", "none", "noop"):
            errors.append(
                "build_mode skip/empty rejected — use jenkins|compile_module|gradle|source_verified "
                "with real evidence (no paper COMPILE_DONE)"
            )
        allowed = {"jenkins", "compile_module", "gradle", "source_verified", "module_incremental"}
        if mode and mode not in allowed:
            errors.append(f"unknown build_mode={mode!r}; allowed={sorted(allowed)}")
        if mode == "jenkins":
            if not (data.get("jenkins") or data.get("job_url") or data.get("build_url")):
                errors.append("jenkins mode requires jenkins object or job_url/build_url")
        if mode in ("compile_module", "module_incremental", "gradle"):
            ok = data.get("success") is True or str(data.get("status") or "").lower() in (
                "success",
                "ok",
                "completed",
                "pass",
            )
            if not ok:
                errors.append(f"{mode} requires success=true or status=success")
            if not (
                data.get("artifact_path")
                or data.get("artifacts")
                or data.get("compile_log")
                or data.get("compile_log_path")
            ):
                errors.append(f"{mode} requires artifact_path/artifacts/compile_log")
        if mode == "source_verified":
            if not data.get("source_file_sha256"):
                errors.append("source_verified requires source_file_sha256 (sha256 of file on build server)")
            if not data.get("source_server_path"):
                errors.append("source_verified requires source_server_path")

    if sid == "10_artifacts":
        arts = data.get("artifacts")
        if not isinstance(arts, list) or not arts:
            errors.append("artifacts[] must be a non-empty array")
        else:
            for i, att in enumerate(arts):
                if not isinstance(att, dict):
                    errors.append(f"artifacts[{i}] must be object")
                    continue
                if att.get("verified") is not True:
                    errors.append(
                        f"artifacts[{i}].verified must be true "
                        "(adb push/pull hash check or local artifact hash after real compile)"
                    )
                if not (att.get("sha256") or att.get("device_sha256") or att.get("local_sha256")):
                    errors.append(f"artifacts[{i}] needs sha256|device_sha256|local_sha256")

    if sid == "11_qfil_flash":
        serial = str(data.get("device_serial") or "").strip()
        if not serial:
            errors.append("device_serial required")
        ctx_ws = {}
        if run_dir:
            ws_path = run_dir / "06_context_snapshot" / "output" / "workspace.json"
            if ws_path.is_file():
                try:
                    ctx_ws = json.loads(ws_path.read_text(encoding="utf-8"))
                except Exception:
                    ctx_ws = {}
            expect = str(ctx_ws.get("device_serial") or "").strip()
            if expect and serial and serial != expect:
                errors.append(f"device_serial {serial!r} != workspace {expect!r}")
            # device_label_verified hard gate (also enforced at agent_gate entry)
            if context and isinstance(context.get("source"), dict):
                if context["source"].get("device_label_verified") is not True:
                    errors.append(
                        "context.source.device_label_verified must be true before flash"
                    )
        skipped = data.get("qfil_skipped") is True
        flash_mode = str(data.get("flash_mode") or "").strip().lower()
        if skipped or flash_mode in ("adb_push", "skip", "none"):
            if not str(data.get("skip_reason") or "").strip() and skipped:
                errors.append("qfil_skipped=true requires skip_reason")
            dv = data.get("device_verification") if isinstance(data.get("device_verification"), dict) else {}
            if dv.get("verified_on_device") is not True:
                errors.append(
                    "non-QFIL flash requires device_verification.verified_on_device=true"
                )

    if sid == "12_cit_test":
        if context and isinstance(context.get("source"), dict):
            if context["source"].get("device_label_verified") is not True:
                errors.append(
                    "context.source.device_label_verified must be true before verify"
                )
        marker = str(data.get("marker") or "").strip().upper()
        auto_a = _assertion_map(data, "auto_assertions")
        # Backward compat: flat assertions → treat as auto unless human_* keys present
        flat = _assertion_map(data, "assertions")
        if not auto_a and flat:
            auto_a = {
                k: v
                for k, v in flat.items()
                if not (
                    isinstance(v, dict)
                    and (
                        v.get("passed") is None
                        or "manual" in str(v.get("note") or "").lower()
                        or "requires manual" in str(v.get("note") or "").lower()
                    )
                )
            }
            # If splitting removed everything, keep explicit auto-only passed=true entries
            if not auto_a:
                auto_a = {
                    k: v
                    for k, v in flat.items()
                    if isinstance(v, dict) and v.get("passed") is True
                }
        if marker == "VERIFY_PASS":
            ok, a_errs = _all_assertions_passed(auto_a)
            if not ok:
                errors.extend(a_errs or ["VERIFY_PASS requires all auto_assertions.passed=true"])
            # Disallow null-passed entries inside claimed auto set
            for name, body in flat.items():
                if isinstance(body, dict) and body.get("passed") is None and name in auto_a:
                    errors.append(f"assertions.{name}.passed is null but counted as auto")
        elif marker == "VERIFY_FAIL":
            pass  # fail marker is valid output but stage should not complete — handled below
        else:
            errors.append("marker must be VERIFY_PASS or VERIFY_FAIL")
        if marker == "VERIFY_FAIL":
            errors.append("VERIFY_FAIL blocks stage completion — fix product or re-analyze")

    if sid == "13_human_gate":
        gs = str(data.get("gate_status") or "").strip().lower()
        allowed_gs = {
            "not_required",
            "auto_bypassed",
            "pending_human",
            "approved",
            "rejected",
            "escalated",
        }
        if gs not in allowed_gs:
            errors.append(f"gate_status must be one of {sorted(allowed_gs)} (got {gs!r})")
        mode = verify_mode or str(data.get("verification_mode") or "").strip().lower()
        if mode not in VERIFY_MODES:
            # Infer: pending/approved ⇒ human path
            if gs in ("pending_human", "approved", "rejected"):
                mode = "human"
            elif gs in ("not_required", "auto_bypassed"):
                mode = "auto"
            else:
                errors.append(
                    "verification_mode/routing.verify_mode missing; "
                    "set context.routing.verify_mode to auto|human|hybrid"
                )
        if mode == "auto":
            if gs not in ("not_required", "auto_bypassed"):
                errors.append(
                    f"verify_mode=auto requires gate_status not_required|auto_bypassed (got {gs!r})"
                )
        elif mode in ("human", "hybrid"):
            if gs == "pending_human":
                errors.append(
                    "gate_status=pending_human — waiting for structured human approval; "
                    "stage cannot complete"
                )
            elif gs in ("rejected", "escalated"):
                errors.append(f"gate_status={gs} blocks closure")
            elif gs != "approved":
                errors.append(
                    f"verify_mode={mode} requires gate_status=approved before complete (got {gs!r})"
                )
            if gs == "approved":
                if not (data.get("approved_by") or data.get("operator")):
                    errors.append("approved gate requires approved_by or operator")

    if sid == "14_closure":
        marker = str(data.get("marker") or "").strip().upper()
        if marker == "CLOSURE_PENDING":
            errors.append("CLOSURE_PENDING is not a completed closure — finish human/commit gates first")
        elif marker and marker != "CLOSURE_DONE":
            errors.append(f"marker must be CLOSURE_DONE (got {marker!r})")
        elif not marker:
            errors.append("marker must be CLOSURE_DONE")

        if not str(data.get("message") or "").strip():
            errors.append("message required (commit / closure summary)")

        mode = str(data.get("closure_mode") or "local_commit_only").strip().lower()
        if mode not in ("full", "local_commit_only", "evidence_only"):
            errors.append(
                f"closure_mode must be full|local_commit_only|evidence_only (got {mode!r})"
            )

        commit = data.get("commit")
        commit_s = str(commit).strip() if commit is not None else ""
        commit_skipped = data.get("commit_skipped") is True
        push_skipped = data.get("push_skipped") is True
        zentao_skipped = data.get("zentao_skipped") is True
        skip_reason = str(data.get("skip_reason") or "").strip()

        if mode == "evidence_only":
            if not skip_reason:
                errors.append("evidence_only requires skip_reason")
            if not (push_skipped and zentao_skipped):
                errors.append("evidence_only requires push_skipped=true and zentao_skipped=true")
            if not commit_skipped and len(commit_s) < 7:
                errors.append(
                    "evidence_only: set commit_skipped=true or provide real commit sha (>=7)"
                )
        elif mode == "local_commit_only":
            if len(commit_s) < 7:
                errors.append("local_commit_only requires commit sha (>=7 chars)")
            if data.get("pushed") is True:
                errors.append("local_commit_only must not set pushed=true (use full)")
            if not push_skipped:
                errors.append("local_commit_only requires push_skipped=true")
            if not skip_reason:
                errors.append("local_commit_only requires skip_reason")
        else:  # full
            if len(commit_s) < 7:
                errors.append("full mode requires commit sha (>=7 chars)")
            if data.get("pushed") is not True:
                errors.append("full mode requires pushed=true")
            if data.get("zentao_updated") is not True:
                errors.append("full mode requires zentao_updated=true")
            if push_skipped or zentao_skipped or commit_skipped:
                errors.append("full mode must not set *_skipped flags (use local_commit_only/evidence_only)")

        # Reserved foothold: when pipeline.gates.push_zentao_close_enforced=true,
        # local_commit_only will be rejected (not active while bootstrap).
        # Extension: check context/pipeline in engine when enabling auto close.

        # Upstream human gate
        if run_dir and bug_id:
            hg = run_dir / "13_human_gate" / "output" / f"human_gate_{bug_id}.json"
            if hg.is_file():
                try:
                    hgd = json.loads(hg.read_text(encoding="utf-8"))
                except Exception:
                    hgd = {}
                hgs = str(hgd.get("gate_status") or "").lower()
                mode_v = verify_mode or str(hgd.get("verification_mode") or "").lower()
                if mode_v == "auto" and hgs not in ("not_required", "auto_bypassed"):
                    errors.append(f"14 requires human_gate auto bypass (got {hgs!r})")
                if mode_v in ("human", "hybrid") and hgs != "approved":
                    errors.append(f"14 requires human_gate approved (got {hgs!r})")
                # Prefer recording status on closure
                reported = str(data.get("human_gate_status") or "").lower()
                if reported and reported != hgs:
                    errors.append(
                        f"human_gate_status {reported!r} != 13 gate_status {hgs!r}"
                    )
            else:
                errors.append(f"missing upstream human_gate: {hg}")
        # 12 must be VERIFY_PASS
        if run_dir and bug_id:
            rr = run_dir / "12_cit_test" / "output" / f"result_{bug_id}.json"
            if rr.is_file():
                try:
                    rd = json.loads(rr.read_text(encoding="utf-8"))
                except Exception:
                    rd = {}
                if str(rd.get("marker") or "").upper() != "VERIFY_PASS":
                    errors.append("14 requires 12 marker VERIFY_PASS")
            else:
                errors.append(f"missing upstream result: {rr}")

    return errors

