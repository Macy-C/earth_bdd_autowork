from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.evidence_compilation import (
    EVIDENCE_COMPILATION_VERSION,
    load_evidence_compilation_result,
)
from autowork_core.utils.debug_tools.recorder.evidence_compiler import (
    build_compilation_from_projection,
)
from autowork_core.utils.debug_tools.recorder.identity import stable_digest
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.projection_store import (
    ProjectionStore,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


EVIDENCE_COMPILATION_SHADOW_VERSION = "1.0"
SHADOW_ROOT = Path("shadow") / "evidence-compilation"
RUN_REPORT_VERSION = "1.0"


def publish_evidence_compilation_shadow_report(take_dir):
    take_dir = Path(take_dir).resolve()
    started_at = datetime.now().isoformat(timespec="milliseconds")
    started = time.monotonic()
    try:
        authoritative = load_evidence_compilation_result(take_dir)
        projection = ProjectionStore(take_dir).current()
        if projection is None:
            raise FileNotFoundError("current projection not available")
        shadow_result = build_compilation_from_projection(take_dir, projection)
        comparison = compare_evidence_compilation_results(
            authoritative,
            shadow_result,
        )
        status = "matched" if not comparison["discrepancies"] else "mismatched"
        report = _base_report(
            take_dir,
            status=status,
            started_at=started_at,
            started=started,
            projection=projection,
            authoritative=authoritative,
            shadow_result=shadow_result,
            comparison=comparison,
        )
    except Exception as error:
        report = _base_report(
            take_dir,
            status="failed",
            started_at=started_at,
            started=started,
            error=error,
        )
    path = _write_shadow_report(take_dir, report)
    report["path"] = path.relative_to(take_dir).as_posix()
    write_json_atomic(path, report)
    return report


def build_evidence_compilation_shadow_run_report(
        session_dir,
        *,
        create_missing=False,
    ):
    session_dir = Path(session_dir).resolve()
    started = time.monotonic()
    takes = []
    generated_count = 0
    for entry in _context_take_entries(session_dir):
        item, generated = _shadow_run_take_item(
            session_dir,
            entry,
            create_missing=create_missing,
        )
        takes.append(item)
        if generated:
            generated_count += 1
    counts = _shadow_run_counts(takes)
    report = {
        "schema_version": SCHEMA_VERSION,
        "evidence_compilation_shadow_run_report_version": RUN_REPORT_VERSION,
        "evidence_compilation_shadow_version": (
            EVIDENCE_COMPILATION_SHADOW_VERSION
        ),
        "mode": "shadow",
        "status": _shadow_run_status(counts),
        "created_at": datetime.now().isoformat(timespec="milliseconds"),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "session_id": _session_id(session_dir),
        "session_path": session_dir.as_posix(),
        "create_missing": bool(create_missing),
        "generated_missing_reports": generated_count,
        "counts": counts,
        "takes": takes,
    }
    report["report_id"] = "evidence-compilation-shadow-run-" + stable_digest(
        report.get("session_id"),
        report.get("status"),
        report.get("created_at"),
        counts,
        length=16,
    )
    return report


def publish_evidence_compilation_shadow_run_report(
        session_dir,
        *,
        create_missing=False,
    ):
    session_dir = Path(session_dir).resolve()
    report = build_evidence_compilation_shadow_run_report(
        session_dir,
        create_missing=create_missing,
    )
    path = session_dir / SHADOW_ROOT / f"{report['report_id']}.json"
    report["path"] = path.relative_to(session_dir).as_posix()
    write_json_atomic(path, report)
    return report


def compare_evidence_compilation_results(authoritative, shadow_result):
    authoritative_stable = _stable_compilation_view(authoritative)
    shadow_stable = _stable_compilation_view(shadow_result)
    discrepancies = []
    for key in sorted(set(authoritative_stable) | set(shadow_stable)):
        if authoritative_stable.get(key) != shadow_stable.get(key):
            discrepancies.append({
                "field": key,
                "authoritative": authoritative_stable.get(key),
                "shadow": shadow_stable.get(key),
            })
    return {
        "authoritative": authoritative_stable,
        "shadow": shadow_stable,
        "discrepancies": discrepancies,
    }


def _context_take_entries(session_dir):
    context = _read_json_optional(Path(session_dir) / "ai" / "context.json")
    entries = []
    for step in context.get("steps") or ():
        if not isinstance(step, dict):
            continue
        step_plan = step.get("step") or {}
        artifacts = step.get("artifacts") or {}
        take_path = str(artifacts.get("take") or "")
        if not take_path:
            continue
        entries.append({
            "step_id": str(step_plan.get("id") or ""),
            "step_text": str(step_plan.get("text") or ""),
            "take_path": take_path,
        })
    return entries


def _shadow_run_take_item(session_dir, entry, *, create_missing):
    take_path = str(entry.get("take_path") or "")
    take_dir = (Path(session_dir) / take_path).resolve()
    report_path = _latest_shadow_report_path(take_dir)
    generated = False
    if report_path is None and create_missing:
        report = publish_evidence_compilation_shadow_report(take_dir)
        report_path = take_dir / str(report.get("path") or "")
        generated = True
    elif report_path is not None:
        report = _read_json_optional(report_path)
    else:
        report = None
    authoritative = _load_authoritative_for_run_item(take_dir)
    item = {
        "step_id": entry.get("step_id"),
        "step_text": entry.get("step_text"),
        "take_path": take_path,
        "take_id": _take_id(take_dir),
        "status": "missing",
        "fresh": False,
        "generated": generated,
        "report_path": None,
        "duration_ms": None,
        "discrepancy_count": None,
        "authoritative_compilation_id": authoritative.get("compilation_id"),
        "authoritative_input_fingerprint": authoritative.get(
            "input_fingerprint"
        ),
    }
    if report_path is not None and report:
        item.update({
            "status": str(report.get("status") or "invalid"),
            "fresh": _shadow_report_is_fresh(report, authoritative),
            "report_path": report_path.relative_to(session_dir).as_posix(),
            "duration_ms": report.get("duration_ms"),
            "discrepancy_count": len(
                ((report.get("comparison") or {}).get("discrepancies") or [])
            ),
            "shadow_report_id": report.get("report_id"),
        })
        if report.get("error"):
            item["error"] = dict(report.get("error") or {})
    if authoritative.get("error"):
        item["authoritative_error"] = authoritative["error"]
    return item, generated


def _latest_shadow_report_path(take_dir):
    root = Path(take_dir) / SHADOW_ROOT
    if not root.is_dir():
        return None
    reports = [
        path for path in root.glob("*.json")
        if path.name.startswith("evidence-compilation-shadow-")
    ]
    if not reports:
        return None
    return max(reports, key=lambda path: path.stat().st_mtime_ns)


def _load_authoritative_for_run_item(take_dir):
    try:
        return load_evidence_compilation_result(take_dir)
    except Exception as error:
        return {
            "error": {
                "type": type(error).__name__,
                "message": str(error),
            }
        }


def _shadow_report_is_fresh(report, authoritative):
    if not authoritative or authoritative.get("error"):
        return False
    return all((
        report.get("authoritative_compilation_id")
        == authoritative.get("compilation_id"),
        report.get("authoritative_input_fingerprint")
        == authoritative.get("input_fingerprint"),
    ))


def _shadow_run_counts(takes):
    statuses = {}
    for item in takes:
        status = str(item.get("status") or "missing")
        statuses[status] = statuses.get(status, 0) + 1
    stale = sum(
        1 for item in takes
        if item.get("status") != "missing" and not item.get("fresh")
    )
    durations = [
        int(item.get("duration_ms") or 0)
        for item in takes
        if item.get("duration_ms") is not None
    ]
    return {
        "take_count": len(takes),
        "status_counts": statuses,
        "missing": statuses.get("missing", 0),
        "matched": statuses.get("matched", 0),
        "mismatched": statuses.get("mismatched", 0),
        "failed": statuses.get("failed", 0),
        "stale": stale,
        "fresh": sum(1 for item in takes if item.get("fresh")),
        "generated": sum(1 for item in takes if item.get("generated")),
        "max_duration_ms": max(durations) if durations else None,
    }


def _shadow_run_status(counts):
    take_count = int((counts or {}).get("take_count") or 0)
    if take_count == 0:
        return "no_takes"
    if counts.get("missing") or counts.get("stale"):
        return "incomplete"
    if counts.get("failed"):
        return "failed"
    if counts.get("mismatched"):
        return "mismatched"
    if counts.get("matched") == take_count:
        return "matched"
    return "incomplete"


def _base_report(
        take_dir,
        *,
        status,
        started_at,
        started,
        projection=None,
        authoritative=None,
        shadow_result=None,
        comparison=None,
        error=None,
    ):
    finished_at = datetime.now().isoformat(timespec="milliseconds")
    report = {
        "schema_version": SCHEMA_VERSION,
        "evidence_compilation_shadow_version": (
            EVIDENCE_COMPILATION_SHADOW_VERSION
        ),
        "evidence_compilation_version": EVIDENCE_COMPILATION_VERSION,
        "mode": "shadow",
        "status": status,
        "created_at": finished_at,
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "take_id": _take_id(take_dir),
        "projection": _projection_summary(projection),
        "authoritative_compilation_id": (
            (authoritative or {}).get("compilation_id")
        ),
        "authoritative_input_fingerprint": (
            (authoritative or {}).get("input_fingerprint")
        ),
        "shadow_compilation_id": (shadow_result or {}).get("compilation_id"),
        "shadow_input_fingerprint": (
            (shadow_result or {}).get("input_fingerprint")
        ),
        "comparison": comparison or {
            "authoritative": {},
            "shadow": {},
            "discrepancies": [],
        },
    }
    if error is not None:
        report["error"] = {
            "type": type(error).__name__,
            "message": str(error),
        }
    report["report_id"] = "evidence-compilation-shadow-" + stable_digest(
        report.get("take_id"),
        report.get("status"),
        report.get("authoritative_compilation_id"),
        report.get("shadow_compilation_id"),
        report.get("created_at"),
        length=16,
    )
    return report


def _write_shadow_report(take_dir, report):
    directory = Path(take_dir).resolve() / SHADOW_ROOT
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{report['report_id']}.json"


def _stable_compilation_view(result):
    result = result if isinstance(result, dict) else {}
    return {
        "status": result.get("status"),
        "terminal": bool(result.get("terminal")),
        "evidence_compiled": bool(result.get("evidence_compiled")),
        "input_fingerprint": result.get("input_fingerprint"),
        "output_fingerprint": result.get("output_fingerprint"),
        "compiled_artifacts": result.get("compiled_artifacts") or {},
        "issues": _issue_facts(result.get("issues") or ()),
        "review_required": _issue_facts(
            result.get("review_required") or ()
        ),
        "hard_missing": _issue_facts(result.get("hard_missing") or ()),
    }


def _issue_facts(items):
    return sorted(
        (
            {
                key: str(item.get(key) or "")
                for key in ("code", "action_id", "event_id", "message")
                if item.get(key) not in (None, "")
            }
            for item in items
            if isinstance(item, dict)
        ),
        key=lambda item: json.dumps(
            item,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _projection_summary(projection):
    if projection is None:
        return None
    return {
        "projection_version": projection.projection_version,
        "projection_revision": projection.projection_revision,
        "source_revision": projection.source_revision,
        "path": projection.directory.name,
        "artifacts": dict(projection.artifacts),
    }


def _take_id(take_dir):
    try:
        value = json.loads((Path(take_dir) / "take.json").read_text(
            encoding="utf-8",
        ))
    except (OSError, json.JSONDecodeError):
        return None
    return str(value.get("id") or "") or None


def _session_id(session_dir):
    manifest = _read_json_optional(Path(session_dir) / "manifest.json")
    return str(manifest.get("session_id") or "") or None


def _read_json_optional(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Summarize non-authoritative evidence compilation shadow reports.",
    )
    parser.add_argument("session_dir")
    parser.add_argument(
        "--create-missing",
        action="store_true",
        help="create missing per-take shadow reports before summarizing",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write the run-level shadow summary under the session shadow directory",
    )
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args(argv)
    if args.write:
        report = publish_evidence_compilation_shadow_run_report(
            args.session_dir,
            create_missing=args.create_missing,
        )
    else:
        report = build_evidence_compilation_shadow_run_report(
            args.session_dir,
            create_missing=args.create_missing,
        )
    print(json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        **({"separators": (",", ":")} if args.compact else {"indent": 2}),
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
