"""Unified CLI entrypoint for `python -m autowork_core.ui_inventory`."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
from typing import List, Optional

from autowork_core.ui_inventory.auditor import analyze_diff_impact, audit_bdd_locators
from autowork_core.ui_inventory.linker import load_and_build_store
from autowork_core.ui_inventory.md_exporter import GLOBAL_INDEX_FILENAME, export_kb_markdown_docs
from autowork_core.ui_inventory.remote_poller import (
    DEFAULT_CLONE_TIMEOUT_SEC,
    DEFAULT_FETCH_TIMEOUT_SEC,
    poll_and_sync_repos,
)
from autowork_core.ui_inventory.scanner.repo_scanner import scan_repository, write_snapshot_file
from autowork_core.ui_inventory.store import (
    SnapshotStore,
    StoreLock,
    StoreLockError,
    export_standalone_scanner_pyz,
    load_repos_config,
    load_repos_config_from_settings,
    resolve_store_dir,
    safe_replace_path,
    safe_rmtree,
)
from autowork_core.ui_inventory.syncer import sync_kb_docs_directory
from autowork_core.ui_inventory.xlsx_writer import write_audit_workbook


def _effective_locale(cli_locale: str = "") -> str:
    if str(cli_locale or "").strip():
        return str(cli_locale).strip()
    try:
        from config.settings import settings
        return str(getattr(settings, "ui_inventory_default_locale", "en-US") or "en-US").strip()
    except Exception:
        return "en-US"


def _atomic_replace_dir(tmp_dir: Path, target_dir: Path) -> None:
    """Atomically swap a fully populated staging directory into `target_dir`."""
    backup_dir = target_dir.parent / f".{target_dir.name}.old"
    if backup_dir.exists():
        safe_rmtree(backup_dir)
    moved_old = False
    try:
        if target_dir.exists():
            safe_replace_path(target_dir, backup_dir, max_wait_sec=15.0)
            moved_old = True
        safe_replace_path(tmp_dir, target_dir, max_wait_sec=15.0)
    except Exception:
        if moved_old and backup_dir.exists() and not target_dir.exists():
            try:
                safe_replace_path(backup_dir, target_dir, max_wait_sec=10.0)
            except OSError:
                pass
        raise
    finally:
        if backup_dir.exists():
            try:
                safe_rmtree(backup_dir)
            except OSError:
                pass


def _effective_reference_dll_dirs(cli_dirs: Optional[List[str]] = None) -> List[Path]:
    raw: List[str] = list(cli_dirs or [])
    if not raw:
        try:
            from config.settings import settings
            raw = list(getattr(settings, "ui_inventory_reference_dll_dirs", []) or [])
        except Exception:
            raw = []
    return [Path(d) for d in raw if str(d or "").strip()]


def _run_build_exports(
    store: SnapshotStore,
    locale: str = "en-US",
    *,
    build_fingerprint: str = "",
    reference_dll_dirs: Optional[List[Path]] = None,
) -> dict:
    widx = load_and_build_store(store, default_locale=locale, reference_dll_dirs=reference_dll_dirs)
    xlsx_path = store.dist_dir / "ui_inventory_audit.xlsx"
    kb_docs_dir = store.dist_dir / "kb_docs"
    tmp_kb_docs_dir = store.dist_dir / f".kb_docs.tmp.{os.getpid()}"
    if tmp_kb_docs_dir.exists():
        safe_rmtree(tmp_kb_docs_dir)

    try:
        md_report = export_kb_markdown_docs(widx, tmp_kb_docs_dir)
        if not (tmp_kb_docs_dir / GLOBAL_INDEX_FILENAME).is_file():
            raise RuntimeError(f"Markdown export did not produce {GLOBAL_INDEX_FILENAME}")
        _atomic_replace_dir(tmp_kb_docs_dir, kb_docs_dir)
    except Exception:
        if tmp_kb_docs_dir.exists():
            safe_rmtree(tmp_kb_docs_dir)
        raise

    written_xlsx = write_audit_workbook(widx, xlsx_path)

    total_ctrls = sum(len(s.interactive_nodes) for s in widx.projected_surfaces.values())
    zero_idx_ctrls = sum(
        1
        for s in widx.projected_surfaces.values()
        for n in s.interactive_nodes
        if n.solved and not n.solved.uses_index
    )
    summary = {
        "repos": len(widx.repo_manifests),
        "surfaces": len(widx.projected_surfaces),
        "interactive_controls": total_ctrls,
        "zero_index_controls": zero_idx_ctrls,
        "zero_index_ratio": round(zero_idx_ctrls / total_ctrls, 4) if total_ctrls else 1.0,
        "uid_shadow_issues": len(widx.uid_shadow_issues),
        "identity_gap_issues": len(widx.identity_gap_issues),
        "dynamic_unmounted_issues": len(widx.dynamic_unmounted_issues),
        "audit_xlsx": str(written_xlsx),
        "kb_docs_dir": str(kb_docs_dir),
        "kb_docs_count": md_report.total_docs,
        "kb_shared_component_docs": md_report.shared_component_docs,
        "kb_max_section_chars": md_report.max_observed_section_chars,
    }
    if build_fingerprint:
        store.record_build_state(build_fingerprint, summary)
    return summary


def main(argv: Optional[List[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    parser = argparse.ArgumentParser(
        prog="python -m autowork_core.ui_inventory",
        description="CT Static UI Control Inventory & Incremental Sync Pipeline",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # 1. scan-repo / scan
    for scan_alias in ("scan-repo", "scan"):
        p_scan = sub.add_parser(scan_alias, help="Scan a single local repository into a .snapshot.json.gz")
        p_scan.add_argument("--repo-root", required=True, help="Local repository directory")
        p_scan.add_argument("--repo-id", required=True, help="Repository identifier")
        p_scan.add_argument("--branch", default="", help="Optional branch name")
        p_scan.add_argument("--build-list", action="append", default=[], help="Optional BuildList file(s)")
        p_scan.add_argument("--exclude", action="append", default=[], help="Optional path/dir exclusion pattern(s)")
        p_scan.add_argument("--store-dir", "--store", dest="store_dir", default="", help="Store directory (defaults to UI_INVENTORY_SETTING.STORE_DIR)")
        p_scan.add_argument("--out", default="", help="Explicit output .snapshot.json.gz path")

    # 2. scan-local (batch scan local workspace of repos + build)
    p_slocal = sub.add_parser("scan-local", help="Scan all Immediate sub-repos under --workspace-root and build dist/")
    p_slocal.add_argument("--workspace-root", required=True, help="Directory containing multiple local repo folders")
    p_slocal.add_argument("--exclude", action="append", default=[], help="Optional path/dir exclusion pattern(s)")
    p_slocal.add_argument("--ref-dll-dir", action="append", default=[], help="Optional external NuGet/SDK DLL dir(s) for type/template metadata")
    p_slocal.add_argument("--store-dir", "--store", dest="store_dir", default="", help="Target store directory (defaults to UI_INVENTORY_SETTING.STORE_DIR)")
    p_slocal.add_argument("--locale", default="", help="Default UI locale for primary selectors (defaults to UI_INVENTORY_SETTING.DEFAULT_LOCALE)")

    # 3. poll-sync
    p_poll = sub.add_parser("poll-sync", help="Poll remote/local repos via git ls-remote and rescan changed repos only")
    p_poll.add_argument("--config", default="", help="Optional Bdd/config.yaml or repos.yaml path (defaults to Bdd/config.yaml UI_INVENTORY_SETTING)")
    p_poll.add_argument("--store-dir", "--store", dest="store_dir", default="", help="Path to UI inventory store directory (defaults to UI_INVENTORY_SETTING.STORE_DIR)")
    p_poll.add_argument("--kb", default="", help="Optional WeKnora STATIC_UI_DOCS knowledge base name or ID to sync into")
    p_poll.add_argument("--dry-run", action="store_true", help="Run WeKnora sync in dry-run mode without remote writes")
    p_poll.add_argument("--prune-removed", action="store_true", help="Delete remote orphan docs removed locally")
    p_poll.add_argument("--force", action="store_true", help="Force rescan even when commit matches manifest")
    p_poll.add_argument("--no-build", action="store_true", help="Skip automatic build/export after snapshot updates")
    p_poll.add_argument("--no-sync", action="store_true", help="Skip automatic WeKnora sync even when STATIC_UI_DOCS is configured")
    p_poll.add_argument("--concurrency", type=int, default=4, help="Concurrent upload/poll workers for WeKnora sync (default: 4)")
    p_poll.add_argument("--cache-mode", choices=("persistent", "ephemeral"), default="", help="Override UI_INVENTORY_SETTING.REPO_CACHE_MODE")
    p_poll.add_argument("--clone-timeout", type=int, default=DEFAULT_CLONE_TIMEOUT_SEC, help="Timeout in seconds for initial shallow clone (default: 3600)")
    p_poll.add_argument("--fetch-timeout", type=int, default=DEFAULT_FETCH_TIMEOUT_SEC, help="Timeout in seconds for incremental git fetch (default: 600)")
    p_poll.add_argument("--build-list", action="append", default=[], help="Optional extra BuildList file(s)")
    p_poll.add_argument("--ref-dll-dir", action="append", default=[], help="Optional external NuGet/SDK DLL dir(s) for type/template metadata")
    p_poll.add_argument("--report-out", default="", help="Optional UTF-8 JSON report output path (prints concise summary to stdout)")
    p_poll.add_argument("--repo", action="append", default=[], help="Restrict poll to specific repo_id(s)")

    # 4. build / export-excel
    for build_alias in ("build", "export-excel"):
        p_build = sub.add_parser(build_alias, help="Link snapshots in memory, solve zero-index selectors, and export dist/")
        p_build.add_argument("--store-dir", "--store", dest="store_dir", default="", help="Path to UI inventory store directory (defaults to UI_INVENTORY_SETTING.STORE_DIR)")
        p_build.add_argument("--locale", default="", help="Default UI locale (en-US or zh-CN)")
        p_build.add_argument("--ref-dll-dir", action="append", default=[], help="Optional external NuGet/SDK DLL dir(s) for type/template metadata")

    # 5. audit-locators
    p_audit = sub.add_parser("audit-locators", help="Read-only health check & zero-index upgrade advisor for Bdd/locators/")
    p_audit.add_argument("--store-dir", "--store", dest="store_dir", default="", help="Path to UI inventory store directory (defaults to UI_INVENTORY_SETTING.STORE_DIR)")
    p_audit.add_argument("--locators-dir", default="Bdd/locators", help="Path to Bdd/locators directory (read-only)")
    p_audit.add_argument("--locale", default="", help="Default UI locale")
    p_audit.add_argument("--ref-dll-dir", action="append", default=[], help="Optional external NuGet/SDK DLL dir(s) for type/template metadata")

    # 6. diff-impact
    p_diff = sub.add_parser("diff-impact", help="Analyze UI control changes and impacted BDD locators/steps between two stores")
    p_diff.add_argument("--old-store", required=True, help="Previous store directory")
    p_diff.add_argument("--new-store", required=True, help="Updated store directory")
    p_diff.add_argument("--bdd-dir", default="Bdd", help="Optional Bdd directory for reverse impact lookup")
    p_diff.add_argument("--locale", default="", help="Default UI locale")
    p_diff.add_argument("--ref-dll-dir", action="append", default=[], help="Optional external NuGet/SDK DLL dir(s) for type/template metadata")

    # 7. sync (WeKnora STATIC_UI_DOCS differential sync)
    p_sync = sub.add_parser("sync", help="Differential SHA-256 upload-verify-then-delete sync of dist/kb_docs/ to WeKnora")
    p_sync.add_argument("--store-dir", "--store", dest="store_dir", default="", help="Path to UI inventory store directory (defaults to UI_INVENTORY_SETTING.STORE_DIR)")
    p_sync.add_argument("--kb", default="", help="Target WeKnora STATIC_UI_DOCS knowledge base name or ID")
    p_sync.add_argument("--dry-run", action="store_true", help="Preview sync actions without modifying remote KB")
    p_sync.add_argument("--prune-removed", action="store_true", help="Delete remote orphan docs absent from dist/kb_docs/")
    p_sync.add_argument("--concurrency", type=int, default=4, help="Concurrent upload/poll workers for WeKnora sync (default: 4)")
    p_sync.add_argument("--allow-cross-role", action="store_true", help="Allow targeting a KB bound to another role")
    p_sync.add_argument("--ref-dll-dir", action="append", default=[], help="Optional external NuGet/SDK DLL dir(s) when auto-building dist/")

    # 8. export-scanner
    p_pyz = sub.add_parser("export-scanner", help="Bundle standalone zero-dependency ct_ui_scanner.pyz")
    p_pyz.add_argument("--out", required=True, help="Output .pyz file path")

    # 9. status
    p_stat = sub.add_parser("status", help="Inspect current store manifest and snapshot summary")
    p_stat.add_argument("--store-dir", "--store", dest="store_dir", default="", help="Path to UI inventory store directory (defaults to UI_INVENTORY_SETTING.STORE_DIR)")

    args = parser.parse_args(argv)

    if args.cmd in ("scan-repo", "scan"):
        repo_root = Path(args.repo_root)
        if not repo_root.is_dir():
            print(f"ERROR: repo-root does not exist: {repo_root}", file=sys.stderr)
            return 2
        excludes = list(args.exclude or [])
        if not excludes:
            try:
                from config.settings import settings as _s
                excludes = list(getattr(_s, "ui_inventory_exclude_path_patterns", []) or [])
            except Exception:
                excludes = []
        snap = scan_repository(
            repo_root=repo_root,
            repo_id=args.repo_id,
            branch=args.branch,
            external_build_lists=[Path(b) for b in args.build_list],
            exclude_path_patterns=excludes,
        )
        if args.out:
            out_path = Path(args.out)
            digest = write_snapshot_file(snap, out_path)
            print(json.dumps({"repo_id": args.repo_id, "out": str(out_path), "sha256": digest, "stats": snap["stats"]}, ensure_ascii=False, indent=2))
            return 0
        store = SnapshotStore(resolve_store_dir(args.store_dir))
        meta = store.save_repo_snapshot(snap)
        print(json.dumps({"repo_id": args.repo_id, "store_dir": str(store.store_dir), **meta}, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "scan-local":
        ws_root = Path(args.workspace_root)
        if not ws_root.is_dir():
            print(f"ERROR: workspace-root does not exist: {ws_root}", file=sys.stderr)
            return 2
        excludes = list(args.exclude or [])
        if not excludes:
            try:
                from config.settings import settings as _s
                excludes = list(getattr(_s, "ui_inventory_exclude_path_patterns", []) or [])
            except Exception:
                excludes = []
        store = SnapshotStore(resolve_store_dir(args.store_dir))
        build_lists = list(ws_root.glob("*/*BuildList*"))
        for sub_dir in sorted(d for d in ws_root.iterdir() if d.is_dir() and not d.name.startswith(".")):
            if "buildscripts" in sub_dir.name.lower():
                continue
            snap = scan_repository(
                sub_dir,
                sub_dir.name,
                external_build_lists=build_lists,
                exclude_path_patterns=excludes,
            )
            store.save_repo_snapshot(snap)
        build_summary = _run_build_exports(
            store,
            locale=_effective_locale(args.locale),
            reference_dll_dirs=_effective_reference_dll_dirs(args.ref_dll_dir),
        )
        print(json.dumps(build_summary, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "poll-sync":
        if args.config:
            cfg = load_repos_config(Path(args.config))
            store = SnapshotStore(resolve_store_dir(args.store_dir, registry=cfg))
        else:
            cfg = load_repos_config_from_settings()
            store = SnapshotStore(resolve_store_dir(args.store_dir, registry=cfg))
            legacy_repos_yaml = store.store_dir / "repos.yaml"
            if not cfg.repos and legacy_repos_yaml.is_file():
                cfg = load_repos_config(legacy_repos_yaml)

        if not cfg.repos:
            print(
                "ERROR: No repositories configured. Add UI_INVENTORY_SETTING.REPOSITORIES in Bdd/config.yaml "
                "or pass --config <path>.",
                file=sys.stderr,
            )
            return 2

        if args.build_list:
            for extra_bl in args.build_list:
                cleaned_bl = str(extra_bl or "").strip()
                if cleaned_bl and cleaned_bl not in cfg.build_lists:
                    cfg.build_lists.append(cleaned_bl)
        if args.ref_dll_dir:
            for extra_rd in args.ref_dll_dir:
                cleaned_rd = str(extra_rd or "").strip()
                if cleaned_rd and cleaned_rd not in cfg.reference_dll_dirs:
                    cfg.reference_dll_dirs.append(cleaned_rd)

        in_ads = bool(os.environ.get("TF_BUILD"))

        def _emit_report_output(payload_dict: dict, exit_code: int) -> int:
            payload_json = json.dumps(payload_dict, ensure_ascii=False, indent=2)
            if args.report_out:
                report_path = Path(args.report_out)
                report_path.parent.mkdir(parents=True, exist_ok=True)
                report_path.write_text(payload_json, encoding="utf-8")
                print("=== UI Inventory Poll-Sync Summary ===")
                print(f"Store dir      : {payload_dict.get('store_dir')}")
                print(f"Cache mode     : {payload_dict.get('repo_cache_mode', '')}")
                print(f"Checked repos  : {payload_dict.get('checked_count', 0)}")
                print(f"Changed repos  : {payload_dict.get('changed_repos', [])}")
                print(f"Unchanged repos: {payload_dict.get('unchanged_repos', [])}")
                if payload_dict.get("removed_repos"):
                    print(f"Removed repos  : {payload_dict['removed_repos']}")
                if payload_dict.get("missing_active_repos"):
                    print(f"Missing active : {payload_dict['missing_active_repos']}")
                if payload_dict.get("gate_status") and payload_dict["gate_status"] != "passed":
                    print(f"Gate status    : {payload_dict['gate_status']}")
                if payload_dict.get("failed_repos"):
                    print(f"Failed repos   : {payload_dict['failed_repos']}")
                    for o in payload_dict.get("outcomes", []):
                        if o.get("status") == "error":
                            print(f"  [ERROR] {o.get('repo_id')}: {o.get('error')}")
                if payload_dict.get("lock_error"):
                    print(f"Lock error     : {payload_dict['lock_error']}")
                if payload_dict.get("message"):
                    print(f"Message        : {payload_dict['message']}")
                for warn in payload_dict.get("warnings", []):
                    print(f"  [WARNING] {warn}")
                    if in_ads:
                        print(f"##vso[task.logissue type=warning]{warn}")
                b = payload_dict.get("build")
                if b:
                    print(
                        f"Build          : repos={b.get('repos')}, surfaces={b.get('surfaces')}, "
                        f"controls={b.get('interactive_controls')}, md_docs={b.get('kb_docs_count')}, "
                        f"max_chunk={b.get('kb_max_section_chars')}"
                    )
                s = payload_dict.get("sync")
                if s:
                    print(
                        f"WeKnora Sync   : kb={s.get('knowledge_base')}, dry_run={s.get('dry_run')}, "
                        f"total={s.get('total_local_docs')}, in_sync={s.get('in_sync_count')}, "
                        f"new={s.get('uploaded_new_count')}, replaced={s.get('replaced_count')}, "
                        f"moved={s.get('moved_count')}, pruned={s.get('pruned_count')}, "
                        f"failed={s.get('failed_count')}"
                    )
                    for kw in s.get("config_warnings", []):
                        print(f"  [KB CONFIG WARNING] {kw}")
                        if in_ads:
                            print(f"##vso[task.logissue type=warning]{kw}")
                    for act in s.get("actions", []):
                        if act.get("action") == "error":
                            print(f"  [SYNC ERROR] {act.get('rel_path')}: {act.get('detail')}")
                if payload_dict.get("sync_error"):
                    print(f"  [SYNC ERROR] {payload_dict['sync_error']}")
                print(f"Full report    : {report_path}")
            else:
                print(payload_json)
            return exit_code

        try:
            lock_ctx = StoreLock(store.store_dir)
            lock_ctx.acquire()
        except StoreLockError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return _emit_report_output(
                {
                    "store_dir": str(store.store_dir),
                    "lock_error": str(exc),
                    "failed_repos": [],
                    "changed_repos": [],
                    "unchanged_repos": [],
                },
                1,
            )

        try:
            # Layer 1: Poll & checkpoint individual repos
            report = poll_and_sync_repos(
                registry=cfg,
                store=store,
                force_rescan=args.force,
                only_repos=args.repo or None,
                repo_cache_mode=args.cache_mode or None,
                clone_timeout_sec=max(30, args.clone_timeout),
                fetch_timeout_sec=max(30, args.fetch_timeout),
                show_progress=True,
            )

            payload = asdict(report)
            payload["store_dir"] = str(store.store_dir)

            # Consistency Gate: NEVER build or sync an incomplete cross-repo view!
            gate_blocked_failed = bool(report.failed_repos)
            gate_blocked_missing = bool(report.missing_active_repos)
            gate_blocked = gate_blocked_failed or gate_blocked_missing
            build_summary: Optional[dict] = None
            sync_summary: Optional[dict] = None
            sync_error_msg = ""

            if gate_blocked_failed:
                payload["gate_status"] = "blocked_by_failed_repos"
                payload["message"] = (
                    f"Skipped build & WeKnora sync because {len(report.failed_repos)} repo(s) failed; "
                    "completed repo snapshots were preserved as checkpoints."
                )
            elif gate_blocked_missing:
                payload["gate_status"] = "blocked_by_missing_active_repos"
                payload["message"] = (
                    f"Skipped build & WeKnora sync because {len(report.missing_active_repos)} active repo(s) "
                    f"lack snapshots ({', '.join(report.missing_active_repos[:6])})."
                )
            else:
                payload["gate_status"] = "passed"
                # Layer 2: Build exports independently if snapshots/locale/builder fingerprint changed or dist is missing
                if not args.no_build:
                    active_ids = [e.id for e in cfg.repos if e.enabled]
                    ref_dll_paths = [Path(d) for d in cfg.reference_dll_dirs if str(d or "").strip()]
                    build_fp = store.compute_build_fingerprint(
                        active_ids,
                        cfg.default_locale,
                        reference_dll_dirs=cfg.reference_dll_dirs,
                    )
                    last_build_fp = store.get_last_build_fingerprint()
                    kb_docs_dir = store.dist_dir / "kb_docs"
                    index_present = (kb_docs_dir / GLOBAL_INDEX_FILENAME).is_file()
                    needs_build = bool(
                        args.force
                        or not index_present
                        or not build_fp
                        or build_fp != last_build_fp
                    )
                    if needs_build:
                        print("[ui-inventory build] Linking snapshots & exporting dist/...", file=sys.stderr, flush=True)
                        build_summary = _run_build_exports(
                            store,
                            locale=cfg.default_locale,
                            build_fingerprint=build_fp,
                            reference_dll_dirs=ref_dll_paths,
                        )
                        payload["build"] = build_summary

                    # Layer 3: Stateless WeKnora convergence (always reconciles empty KB / post-dryRun / interrupted sync)
                    if not args.no_sync:
                        target_kb = args.kb or cfg.weknora_ui_kb_id
                        if target_kb:
                            try:
                                sync_rep = sync_kb_docs_directory(
                                    kb_docs_dir,
                                    kb_selector=target_kb,
                                    dry_run=args.dry_run,
                                    prune_removed=args.prune_removed,
                                    max_workers=max(1, args.concurrency),
                                    show_progress=not args.dry_run,
                                )
                                sync_summary = asdict(sync_rep)
                                payload["sync"] = sync_summary
                            except Exception as exc:
                                sync_error_msg = str(exc)
                                payload["sync_error"] = sync_error_msg
                                print(f"[ui-inventory sync] ERROR: {exc}", file=sys.stderr, flush=True)

            if not gate_blocked:
                sync_mutations = 0
                if sync_summary is not None:
                    sync_mutations = (
                        int(sync_summary.get("uploaded_new_count", 0))
                        + int(sync_summary.get("replaced_count", 0))
                        + int(sync_summary.get("moved_count", 0))
                        + int(sync_summary.get("pruned_count", 0))
                        + int(sync_summary.get("failed_count", 0))
                    )
                if not report.changed_repos and not report.removed_repos and not sync_error_msg:
                    if sync_mutations == 0:
                        payload["message"] = "All repositories up-to-date"
                    else:
                        payload["message"] = "Repositories unchanged; reconciled WeKnora STATIC_UI_DOCS"

            sync_failed = bool(sync_error_msg) or (
                sync_summary is not None and int(sync_summary.get("failed_count", 0)) > 0
            )
            # Single-repo debug (`--repo X`) on a partially cold store exits 0 if `X` succeeded;
            # full polls with any missing or failed repo exit 1.
            partial_single_repo_only = bool(args.repo) and not gate_blocked_failed and gate_blocked_missing
            should_fail = (
                gate_blocked_failed
                or (gate_blocked_missing and not partial_single_repo_only)
                or sync_failed
            )
            return _emit_report_output(payload, 1 if should_fail else 0)
        finally:
            lock_ctx.release()

    if args.cmd in ("build", "export-excel"):
        store = SnapshotStore(resolve_store_dir(args.store_dir))
        summary = _run_build_exports(
            store,
            locale=_effective_locale(args.locale),
            reference_dll_dirs=_effective_reference_dll_dirs(args.ref_dll_dir),
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "audit-locators":
        store = SnapshotStore(resolve_store_dir(args.store_dir))
        locale = _effective_locale(args.locale)
        ref_dirs = _effective_reference_dll_dirs(args.ref_dll_dir)
        widx = load_and_build_store(store, default_locale=locale, reference_dll_dirs=ref_dirs)
        items = audit_bdd_locators(widx, Path(args.locators_dir), default_locale=locale)
        by_status: dict = {}
        for it in items:
            by_status[it.status] = by_status.get(it.status, 0) + 1
        print(
            json.dumps(
                {
                    "store_dir": str(store.store_dir),
                    "total_xpath_locators": len(items),
                    "by_status": by_status,
                    "items": [asdict(x) for x in items],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.cmd == "diff-impact":
        bdd_p = Path(args.bdd_dir) if args.bdd_dir else None
        rep = analyze_diff_impact(
            SnapshotStore(Path(args.old_store)),
            SnapshotStore(Path(args.new_store)),
            bdd_dir=bdd_p if bdd_p and bdd_p.is_dir() else None,
            default_locale=_effective_locale(args.locale),
            reference_dll_dirs=_effective_reference_dll_dirs(args.ref_dll_dir),
        )
        print(json.dumps(asdict(rep), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "sync":
        store = SnapshotStore(resolve_store_dir(args.store_dir))
        kb_docs_dir = store.dist_dir / "kb_docs"
        if not kb_docs_dir.is_dir():
            _run_build_exports(
                store,
                locale=_effective_locale(),
                reference_dll_dirs=_effective_reference_dll_dirs(args.ref_dll_dir),
            )
        sync_rep = sync_kb_docs_directory(
            kb_docs_dir,
            kb_selector=args.kb or None,
            dry_run=args.dry_run,
            max_workers=max(1, args.concurrency),
            show_progress=not args.dry_run,
            prune_removed=args.prune_removed,
            allow_cross_role=args.allow_cross_role,
        )
        print(json.dumps(asdict(sync_rep), ensure_ascii=False, indent=2))
        return 1 if sync_rep.failed_count > 0 else 0

    if args.cmd == "export-scanner":
        out_pyz = Path(args.out)
        digest = export_standalone_scanner_pyz(out_pyz)
        print(json.dumps({"out": str(out_pyz), "sha256": digest}, ensure_ascii=False))
        return 0

    if args.cmd == "status":
        store = SnapshotStore(resolve_store_dir(args.store_dir))
        print(json.dumps({"store_dir": str(store.store_dir), **store.load_manifest()}, ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
