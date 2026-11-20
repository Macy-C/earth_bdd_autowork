from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

from autowork_core.knowledge.case_export import build_standalone_case
from autowork_core.knowledge.case_context import (
    inspect_capture_for_polish,
    scan_captures,
)
from autowork_core.knowledge.case_publish import REVIEW_CHECKS, accept_case_markdown, case_processing_status, clean_capture_directory, preflight_publish, publish_case_markdown, retire_case_versions
from autowork_core.knowledge.weknora_client import PublishError
from autowork_core.knowledge.case_verifier import verify_case_markdown


def _next_action_for_error(error):
    guidance = {
        "configuration_error": "Set WEKNORA_SETTING.API_URL, API_KEY, and optional KNOWLEDGE_BASES in Bdd/config.yaml, or pass --kb.",
        "knowledge_base_not_unique": "Use the exact knowledge-base UUID with --kb, or correct the knowledge-base name.",
        "knowledge_base_role_mismatch": "Use the knowledge base bound to this workflow role (ENGINEERING_CASES for case operations, FRAMEWORK_DOCS for document-sync).",
        "http_error": "Check the reported HTTP status, REST API key scope, target knowledge-base permission, and server response reason before retrying.",
        "connection_failed": "Check WEKNORA_SETTING.API_URL, network reachability, and TLS/proxy settings; no blind upload retry was made.",
        "invalid_document": "Fix the listed structural verification errors or choose the correct finished case artifact.",
        "source_changed": "Re-inspect and re-polish the current source file; the earlier artifact or specification no longer matches it.",
        "polish_not_completed": "Complete the protected file-polishing work item, mark its reviewer, and then retry the requested upload.",
        "polish_token_changed": "Restore every protected action, image, control, URL, and code token in the replacement prose.",
        "upload_outcome_unknown": "Run status for the same artifact after connectivity is restored; do not upload a second copy yet.",
        "publication_conflict": "Inspect the reported candidates for this recorded submission; no document was selected and no upload was retried.",
        "publication_record_failed": "Restore local recovery-record storage, preserve any returned document ID, then run status for the same artifact before resuming publish.",
        "upload_conflict": "Inspect the reported remote conflict; the source and previous remote version were preserved.",
        "wiki_pending": "Run status later for the same artifact; do not accept or clean it yet.",
        "retirement_pending": "Run status later until the acknowledged old-version delete task has completed.",
    }
    return guidance.get(error.code, "Read the reported stage and message; no automatic cleanup or broad retry was performed.")


def _compact_review_result(result):
    result = dict(result)
    review = result.pop("review", None)
    if review is not None:
        result["review_summary"] = {
            key: review[key] for key in ("status", "api_url", "knowledge_base_id", "document_id", "content_sha256", "checks") if key in review
        }
        result["review_summary"]["page_count"] = len(review.get("pages", []))
    return result


def _main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -B -m autowork_core.knowledge",
        description="BDD Autowork local case export, verification, and optional WeKnora publication.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser(
        "scan",
        help="Scan artifacts/knowledge-captures, filter out failed runs, and deduplicate by (Feature, Scenario).",
    )
    scan_parser.add_argument("--captures-root", default="auto", help="Path to artifacts/knowledge-captures (default: auto)")
    scan_parser.add_argument("--query", default=None, help="Optional keyword filter (Feature code, Scenario name, Product, or Run ID)")

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="Extract Base64-free context and generate polish-spec.json from capture run(s) for AI polishing.",
    )
    inspect_parser.add_argument("--capture-dir", default="auto", help="Path to capture run dir or 'auto' to scan latest passed captures")
    inspect_parser.add_argument("--query", default=None, help="Optional keyword filter (Feature code, Scenario name, or Run ID)")
    inspect_parser.add_argument("--feature-source", default=None, help="Optional Feature relative path filter")
    inspect_parser.add_argument("--scenario-name", default=None, help="Optional Scenario name filter")
    inspect_parser.add_argument("--output-spec", default=None, help="Optional output path for polish-spec.json")
    inspect_parser.add_argument("--compact", action="store_true", help="Return each case once, retaining selection diagnostics")
    inspect_parser.add_argument("--read-only", action="store_true", help="Extract context without creating or updating a polish specification")

    build_parser = subparsers.add_parser(
        "build",
        help="Export case Markdown(s) locally without AI or a knowledge-base connection; polishing is optional.",
    )
    build_parser.add_argument("--capture-dir", default="auto", help="Path to capture run dir or 'auto' to build latest passed captures")
    build_parser.add_argument("--query", default=None, help="Optional keyword filter (Feature code, Scenario name, or Run ID)")
    build_parser.add_argument("--feature-source", default=None, help="Optional Feature relative path filter")
    build_parser.add_argument("--scenario-name", default=None, help="Optional Scenario name filter")
    build_parser.add_argument("--output", default=None, help="Optional explicit output Markdown path")
    build_parser.add_argument("--polish-spec", default=None, help="Optional path to AI-polished polish-spec.json")
    build_parser.add_argument("--method-summary", default=None, help="Optional concise business method summary override")
    build_parser.add_argument("--compact", action="store_true", help="Return each built case once")

    verify_parser = subparsers.add_parser("verify", help="Verify a case Markdown against WeKnora v0.8.3 Wiki rules.")
    verify_parser.add_argument("--markdown", required=True, help="Path to the case Markdown file")
    verify_parser.add_argument("--observations", default=None, help="Optional observations.json to cross-check counts and SHA256")
    verify_parser.add_argument("--max-frame-chars", type=int, default=380, help="Max characters per frame block excluding Base64")

    processing = subparsers.add_parser("processing-status", help="Read processing state through a bound receipt; no download, Wiki, upload or structural verification")
    source = processing.add_mutually_exclusive_group(required=True)
    source.add_argument("--markdown")
    source.add_argument("--receipt")
    processing.add_argument("--kb", "--kb-id", dest="kb_id")
    processing.add_argument("--api-url")

    command_help = {
        "preflight": "Validate explicit WeKnora REST configuration and the target knowledge base.",
        "publish": "Publish or resume exactly one existing Markdown artifact; never blindly repeat an unknown submission.",
        "status": "Read and reconcile a recorded publication without uploading, tagging, or modifying its record.",
        "accept": "Persist a review bound to the current artifact and Wiki pages without deleting old versions.",
        "retire": "Explicitly request retirement of superseded versions for one accepted artifact.",
        "clean": "Delete one completed capture run only after all of its cases are remotely accepted.",
    }
    for command, help_text in command_help.items():
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("--kb", "--kb-id", dest="kb_id", default=None, help="Exact target knowledge-base name or ID")
        command_parser.add_argument("--api-url", default=None, help="Explicit REST server root override")
        if command in {"publish", "status", "accept", "retire"}:
            command_parser.add_argument("--markdown", required=True, help="Exact existing file; it is never regenerated or overwritten")
        if command == "publish":
            command_parser.add_argument("--remote-file-name", default=None, help="Optional root-level Markdown filename; directories are not accepted")
            command_parser.add_argument("--tags", default=None, help="Comma-separated additional tags; feature:<Feature filename stem> is always retained")
            command_parser.add_argument("--dry-run", action="store_true")
        if command in {"publish", "status"}:
            command_parser.add_argument("--review-output", default=None, help="New temporary JSON file for AI Wiki review")
        if command in {"status", "accept", "retire"}:
            command_parser.add_argument("--compact", action="store_true", help="Print review binding instead of duplicating content; status requires --review-output")
        if command == "accept":
            command_parser.add_argument("--review", required=True, help="AI review bound to this document and current Wiki versions")
            command_parser.add_argument("--check", action="append", choices=REVIEW_CHECKS, help="Explicitly passed check; repeat for all four after actual content review")
            command_parser.add_argument("--notes", default=None)
            command_parser.add_argument("--review-sha256", default=None, help="Exact review hash from compact status; required for field submission")
        if command == "clean":
            command_parser.add_argument("--capture-dir", required=True, help="One completed capture run; every case must be remotely accepted")

    args = parser.parse_args(argv)
    if args.command == "status" and args.compact and not args.review_output:
        raise ValueError("Compact full status requires --review-output so review content remains available")
    if args.command == "scan":
        result = scan_captures(
            args.captures_root,
            query=args.query,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "inspect":
        if args.read_only and args.output_spec:
            raise ValueError("Read-only context extraction does not accept an output specification")
        result = inspect_capture_for_polish(
            args.capture_dir,
            query=args.query,
            feature_source=args.feature_source,
            scenario_name=args.scenario_name,
            output_spec_path=args.output_spec,
            persist_spec=not args.read_only,
        )
        if args.compact:
            result = {
                "status": result["status"], "cases": result["cases"],
                "scan_summary": {key: value for key, value in result["scan_summary"].items() if key != "selected_cases"},
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "build":
        result = build_standalone_case(
            args.capture_dir,
            query=args.query,
            feature_source=args.feature_source,
            scenario_name=args.scenario_name,
            output_path=args.output,
            method_summary=args.method_summary,
            polish_spec_path=args.polish_spec,
        )
        if args.compact:
            result = {"status": result["status"], "cases": result["cases"]}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "verify":
        result = verify_case_markdown(
            args.markdown,
            observations_path=args.observations,
            max_frame_chars=args.max_frame_chars,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "passed" else 1

    if args.command == "processing-status":
        result = case_processing_status(markdown_path=args.markdown, receipt_path=args.receipt, kb_id=args.kb_id, api_url=args.api_url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in {"processing", "completed"} else 1 if result["status"] == "processing_failed" else 2
    if args.command == "preflight":
        client, knowledge_base = preflight_publish(kb_id=args.kb_id, api_url=args.api_url)
        result = {
            "status": "ready", "api_url": client.api_url,
            "knowledge_base_id": knowledge_base["id"], "knowledge_base_name": knowledge_base.get("name"),
            "write_permission": "checked_by_server_on_write",
        }
    elif args.command in {"publish", "status"}:
        tags = getattr(args, "tags", None)
        tag_list = [tag.strip() for tag in tags.split(",") if tag.strip()] if tags else None
        result = publish_case_markdown(
            args.markdown, kb_id=args.kb_id, api_url=args.api_url,
            remote_file_name=getattr(args, "remote_file_name", None), tags=tag_list,
            dry_run=getattr(args, "dry_run", False), resume_only=args.command == "status",
            inspect_wiki=args.command == "status",
        )
        if args.review_output and result.get("review"):
            review_path = Path(args.review_output).resolve()
            if review_path == Path(args.markdown).resolve():
                raise ValueError("A review output must not overwrite the selected document")
            review_path.parent.mkdir(parents=True, exist_ok=True)
            with review_path.open("x", encoding="utf-8") as destination:
                json.dump(result["review"], destination, ensure_ascii=False, indent=2)
            result["review_path"] = str(review_path)
            if getattr(args, "compact", False):
                result["review_sha256"] = hashlib.sha256(review_path.read_bytes()).hexdigest()
                result = _compact_review_result(result)
    elif args.command == "accept":
        if args.check is not None:
            if len(args.check) != len(set(args.check)):
                raise ValueError("Each review check must appear only once")
            result = accept_case_markdown(
                args.markdown, args.review, kb_id=args.kb_id, api_url=args.api_url,
                checks=dict.fromkeys(args.check, True), notes=args.notes, expected_review_sha256=args.review_sha256,
            )
        else:
            if args.notes is not None or args.review_sha256 is not None:
                raise ValueError("Review field submission requires --check")
            result = accept_case_markdown(args.markdown, args.review, kb_id=args.kb_id, api_url=args.api_url)
        if args.compact:
            result = _compact_review_result(result)
    elif args.command == "retire":
        result = retire_case_versions(args.markdown, kb_id=args.kb_id, api_url=args.api_url)
        if args.compact:
            result = _compact_review_result(result)
    elif args.command == "clean":
        result = clean_capture_directory(args.capture_dir, kb_id=args.kb_id, api_url=args.api_url)
    else:
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] in {"ready", "uploaded", "accepted", "cleaned", "dry_run"}:
        return 0
    return 1 if result["status"] in {"upload_outcome_unknown", "verification_pending", "upload_rejected"} else 2


def main(argv=None):
    try:
        return _main(argv)
    except PublishError as error:
        result = {
            "status": "blocked",
            "code": error.code,
            "stage": error.stage or "operation",
            "message": str(error),
            "next_action": _next_action_for_error(error),
        }
        if error.details:
            result["details"] = error.details
    except HTTPError as error:
        result = {
            "status": "failed", "code": "http_error", "stage": "operation",
            "http_status": error.code,
            "message": "An unwrapped HTTP error occurred before a structured response was available",
            "next_action": "Inspect the server response and rerun status before attempting another upload.",
        }
    except (URLError, TimeoutError):
        result = {
            "status": "failed", "code": "connection_failed", "stage": "operation",
            "message": "No blind upload retry or cleanup was performed",
            "next_action": "Check network reachability, then run status for the same artifact before retrying upload.",
        }
    except (OSError, ValueError) as error:
        result = {
            "status": "blocked", "code": "invalid_input", "stage": "input validation",
            "message": str(error),
            "next_action": "Correct the indicated path, source file, or option and rerun the same command.",
        }
    except SystemExit as error:
        return int(error.code or 0)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
