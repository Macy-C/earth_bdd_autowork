from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
from http.client import HTTPException
from pathlib import Path, PurePosixPath
from uuid import NAMESPACE_URL, uuid4, uuid5

from autowork_core.knowledge.case_context import derive_case_identity, normalize_case_tags
from autowork_core.knowledge.weknora_client import (
    PublishError,
    RemoteHTTPError,
    WeKnoraClient,
    quote_id,
    resolve_kb_role_selector,
    verify_resolved_kb_role,
)


PRODUCER = "bdd-autowork"
REVIEW_CHECKS = ("action_order", "control_attribution", "images", "inference_boundaries")
HASH_LINE = re.compile(r"(?m)^- 内容指纹（Content Hash）：`[a-fA-F0-9]{64}`\r?\n")
REQUEST_ERRORS = (PublishError, OSError, HTTPException)
SUBMISSION_STATUSES = {"not_sent": "not_submitted", "unknown": "upload_outcome_unknown", "rejected": "upload_rejected"}


def _metadata(document):
    result = {}
    for field in ("metadata", "custom_metadata"):
        value = document.get(field) or {}
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as error:
                raise PublishError("invalid_response", "Malformed remote metadata") from error
        if not isinstance(value, dict):
            raise PublishError("invalid_response", "Remote metadata is not an object")
        result.update(value)
    return result


def _tag_ids(document):
    return set(document.get("tag_ids") or ()) | {
        item["id"] for item in document.get("tags") or () if isinstance(item, dict) and item.get("id")
    }


def _safe_remote_path(value):
    path = PurePosixPath(str(value).replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts or not path.parts or any(character in str(path) for character in '\r\n\x00"') or ":" in str(path):
        raise PublishError("invalid_identity", "Invalid remote file path")
    if path.suffix.lower() != ".md":
        raise PublishError("invalid_identity", "Expected a Markdown remote file name")
    return path.as_posix()


def _publication_path(value, capture_case_id, content_sha256):
    path = PurePosixPath(_safe_remote_path(value))
    return path.with_name(f"{path.stem}--{content_sha256[:16]}--{capture_case_id}{path.suffix}").as_posix()


def _safe_remote_filename(value):
    if "/" in str(value) or "\\" in str(value):
        raise PublishError("invalid_identity", "New case uploads accept a filename only, without directories")
    return _safe_remote_path(value)


def _receipt_path(api_url, kb_id, capture_case_id, content_sha256):
    from config.paths import Paths

    identity = json.dumps([api_url.rstrip("/"), kb_id, capture_case_id, content_sha256], ensure_ascii=False)
    receipt_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return Paths.ARTIFACTS_DIR / "knowledge-publications" / f"{receipt_id}.json"


def _is_pending_publication(record):
    return (
        isinstance(record, dict) and record.get("publication_version") == 2
        and record.get("document_id") is None
        and record.get("upload_outcome") in {"not_sent", "unknown", "rejected"}
        and isinstance(record.get("case_key"), str) and bool(record["case_key"])
        and isinstance(record.get("remote_file_name"), str) and bool(record["remote_file_name"])
    )


def _read_receipt(capture_case_id, kb_id, content_sha256, *, api_url):
    path = _receipt_path(api_url, kb_id, capture_case_id, content_sha256)
    if not path.is_file():
        return None
    try:
        receipt = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise PublishError("invalid_receipt", "Publication receipt is unreadable; no upload retry was attempted", stage="publication receipt") from error
    required = {"api_url": api_url, "capture_case_id": capture_case_id, "knowledge_base_id": kb_id, "content_sha256": content_sha256}
    if not isinstance(receipt, dict) or any(receipt.get(key) != value for key, value in required.items()) or not (
        isinstance(receipt.get("document_id"), str) and bool(receipt["document_id"].strip()) or _is_pending_publication(receipt)
    ):
        raise PublishError("receipt_conflict", "Publication receipt belongs to different content or knowledge base", stage="publication receipt")
    return receipt


def case_processing_status(*, markdown_path=None, receipt_path=None, kb_id=None, api_url=None):
    from autowork_core.knowledge.document_sync import _processing_status, _redact_result
    from config.paths import Paths

    if (markdown_path is None) == (receipt_path is None):
        raise ValueError("Provide exactly one Markdown path or publication receipt")
    receipt = None
    if markdown_path is not None:
        path = Path(markdown_path)
        if not path.is_absolute():
            path = Paths.BASE_DIR / path
        path = path.resolve()
        identity = extract_case_metadata_from_markdown(path)
        content_sha256 = identity["content_sha256"]
        capture_case_id = identity["capture_case_id"] or uuid5(
            NAMESPACE_URL, f"bdd-autowork:explicit-markdown:{path}:{content_sha256}",
        ).hex
    else:
        receipt = json.loads(Path(receipt_path).read_text(encoding="utf-8-sig"))
        if not isinstance(receipt, dict):
            raise PublishError("invalid_receipt", "Expected a publication receipt object", stage="publication receipt")
        capture_case_id = receipt.get("capture_case_id")
        content_sha256 = receipt.get("content_sha256")
        if (not isinstance(capture_case_id, str) or not capture_case_id.strip()
                or not isinstance(content_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", content_sha256)
                or not (isinstance(receipt.get("document_id"), str) and bool(receipt["document_id"].strip()) or _is_pending_publication(receipt))):
            raise PublishError("invalid_receipt", "Publication receipt has invalid identity fields", stage="publication receipt")
    client, knowledge_base = preflight_publish(
        kb_id=kb_id or (receipt or {}).get("knowledge_base_id"),
        api_url=api_url,
        allow_cross_role=True,
    )
    selected_kb = knowledge_base["id"]
    if receipt is None:
        receipt = _read_receipt(capture_case_id, selected_kb, content_sha256, api_url=client.api_url)
    if receipt is None:
        raise PublishError("document_not_found", "No receipt exists for these exact artifact bytes; processing status will not upload", stage="publication receipt")
    if receipt.get("api_url") != client.api_url or receipt.get("knowledge_base_id") != selected_kb:
        raise PublishError("receipt_conflict", "Receipt belongs to another server or knowledge base", stage="publication receipt")
    if receipt.get("document_id") is None:
        return _redact_result({
            "knowledge_base_id": selected_kb, "document_id": None, "read_only": True,
            "verification": "processing_only", "status": SUBMISSION_STATUSES[receipt["upload_outcome"]],
            "upload_outcome": receipt["upload_outcome"],
            "verification_status": receipt.get("verification_status", "pending"),
            "tagging_status": receipt.get("tagging_status", "not_attempted"),
            "next_action": "Use status for the same Markdown to reconcile its recorded identity and bytes; processing-only queries never upload or download files.",
        }, client)
    document = client.document(receipt["document_id"], selected_kb)
    metadata = _metadata(document)
    if metadata.get("capture_case_id") != capture_case_id or metadata.get("content_sha256") != content_sha256:
        raise PublishError("receipt_conflict", "Remote identity does not match the selected publication receipt", stage="processing status")
    return _redact_result({
        "knowledge_base_id": selected_kb, "document_id": document["id"],
        "file_name": document.get("file_name"), "read_only": True,
        "verification": "processing_only", "upload_outcome": receipt.get("upload_outcome", "accepted"),
        "verification_status": receipt.get("verification_status", "verified"),
        "tagging_status": receipt.get("tagging_status", "not_attempted"), **_processing_status(document),
    }, client)


def _write_receipt(capture_case_id, kb_id, content_sha256, document_id, remote_file_name, *, api_url):
    payload = {
        "api_url": api_url,
        "capture_case_id": capture_case_id,
        "knowledge_base_id": kb_id,
        "content_sha256": content_sha256,
        "document_id": document_id,
        "remote_file_name": remote_file_name,
    }
    _save_publication(payload)


def _save_publication(record):
    path = _receipt_path(record["api_url"], record["knowledge_base_id"], record["capture_case_id"], record["content_sha256"])
    temporary = path.with_name(f".{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        raise PublishError(
            "publication_record_failed", "Publication progress could not be saved; no further remote writes were attempted",
            stage="publication record", details={
                "document_id": record.get("document_id"), "upload_outcome": record.get("upload_outcome", "unknown"),
                "verification_status": record.get("verification_status", "pending"),
            },
        ) from error
    finally:
        temporary.unlink(missing_ok=True)


def _reconcile_publication(record, client):
    destination = PurePosixPath(record["remote_file_name"])
    folder = "" if destination.parent == PurePosixPath(".") else destination.parent.as_posix()
    query = urllib.parse.urlencode({"keyword": destination.name, "folder_path": folder})
    candidates = []
    for item in client.records(f"/knowledge-bases/{quote_id(record['knowledge_base_id'])}/knowledge?{query}"):
        if item.get("file_name") != destination.name or (item.get("folder_path") or "") != folder:
            continue
        document = client.document(item["id"], record["knowledge_base_id"])
        metadata = _metadata(document)
        expected = {
            "producer": PRODUCER, "case_key": record["case_key"],
            "capture_case_id": record["capture_case_id"], "content_sha256": record["content_sha256"],
        }
        if all(metadata.get(key) == value for key, value in expected.items()):
            candidates.append(document["id"])
    if len(candidates) != 1:
        raise PublishError(
            "publication_conflict" if candidates else "upload_outcome_unknown",
            "The recorded submission does not have exactly one matching remote document; no upload was retried",
            stage="publication reconciliation", details={"candidate_document_ids": candidates},
        )
    return candidates[0]


def _publication_result(result, record, status, *, document=None, error=None, read_only=False):
    from autowork_core.knowledge.document_sync import _processing_status

    processing = _processing_status(document) if document is not None else {
        "status": "not_checked", "parse_status": "unknown", "summary_status": "unknown",
    }
    response = {
        **result, "status": status, "knowledge_base_id": record["knowledge_base_id"],
        "weknora_document_id": record.get("document_id"),
        "upload_outcome": record.get("upload_outcome", "accepted"),
        "verification_status": record.get("verification_status", "verified"),
        "tagging_status": record.get("tagging_status", "not_attempted"),
        "parse_status": processing["parse_status"], "summary_status": processing["summary_status"],
        "processing": processing, "read_only": read_only,
        "recovery_record": str(_receipt_path(record["api_url"], record["knowledge_base_id"], record["capture_case_id"], record["content_sha256"])),
    }
    if error is not None:
        response["error"] = error
    if "tag_ids" in record:
        response["tag_ids"] = list(record["tag_ids"])
    if status in {"upload_outcome_unknown", "verification_pending"}:
        response["next_action"] = "Run status for this same artifact to verify the recorded submission; publish resumes it without a second file upload."
    elif status in {"not_submitted", "upload_rejected"}:
        response["next_action"] = "No file was accepted for this submission. Resolve any reported rejection, then explicitly publish the same artifact."
    return response


def extract_case_metadata_from_markdown(markdown_path, *, payload_bytes=None):
    path = Path(markdown_path).resolve()
    content = path.read_bytes() if payload_bytes is None else payload_bytes
    text = content.decode("utf-8-sig")
    header = text.split("\n## 2.", 1)[0]

    def field(label):
        match = re.search(re.escape(label) + r"[^`\r\n]*`([^`\r\n]+)`", header)
        return match.group(1).strip() if match else None

    feature_source = field("Feature 身份")
    scenario_name = field("Scenario：")
    case_key = field("案例唯一键（Case Key）")
    if case_key and "::" in case_key:
        key_source, key_scenario = case_key.split("::", 1)
        if (feature_source and feature_source != key_source) or (scenario_name and scenario_name != key_scenario):
            raise PublishError("invalid_identity", "Conflicting case identity fields")
        feature_source, scenario_name = key_source, key_scenario
    if not feature_source or not scenario_name:
        raise PublishError("invalid_identity", "The document must identify its Feature and exactly one Scenario; its filename is not an identity")
    identity = derive_case_identity(feature_source, PurePosixPath(feature_source).stem, scenario_name)
    declared_fingerprint = field("内容指纹（Content Hash）")
    body_fingerprint = hashlib.sha256(HASH_LINE.sub("", text).encode("utf-8")).hexdigest()
    tags_match = re.search(r"(?m)^- 文档分类标签[^\r\n]+", header)
    tags = [
        value[1:-1] if value.startswith(" ") and value.endswith(" ") and value.strip(" ") else value
        for _marker, value in re.findall(r"(?<!`)(`+)(?!`)(.*?)\1(?!`)", tags_match.group(0))
    ] if tags_match else identity["tags"]
    try:
        tags = normalize_case_tags(identity["feature_code"], tags)
    except ValueError as error:
        raise PublishError("invalid_identity", str(error)) from error
    legacy_destination = field("远端归档路径")
    suggested_name = field("建议上传文件名") or (
        PurePosixPath(_safe_remote_path(legacy_destination)).name if legacy_destination else identity["remote_file_name"]
    )
    return {
        **identity,
        "run_id": field("Run ID：") or "unknown",
        "capture_case_id": field("采集案例 ID（Capture Case ID）") or "",
        "remote_file_name": _safe_remote_filename(suggested_name),
        "tags": list(dict.fromkeys(tags)),
        "evidence_sha256": field("证据指纹（Evidence Hash）") or "",
        "declared_body_fingerprint": declared_fingerprint,
        "body_fingerprint": body_fingerprint,
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "size_bytes": len(content),
    }


def preflight_publish(
    *, kb_id=None, api_url=None, timeout=120,
    expected_role="ENGINEERING_CASES", allow_cross_role=False,
):
    from config.settings import settings

    address = api_url or settings.weknora_api_url
    if not address:
        raise PublishError("configuration_error", "Set WEKNORA_SETTING.API_URL in Bdd/config.yaml", stage="preflight")
    selector = resolve_kb_role_selector(
        kb_id, expected_role=expected_role, allow_cross_role=allow_cross_role,
    )
    token = settings.weknora_api_key
    if not token:
        raise PublishError("configuration_error", "Set WEKNORA_SETTING.API_KEY in Bdd/config.yaml", stage="preflight")
    client = WeKnoraClient(address, token, timeout=timeout)
    records = client.records("/knowledge-bases")
    matches = [item for item in records if selector in {item.get("id"), item.get("name")}]
    if len(matches) != 1:
        raise PublishError("knowledge_base_not_unique", "The requested knowledge base must resolve to exactly one accessible ID", stage="knowledge-base lookup")
    verify_resolved_kb_role(
        matches[0], expected_role=expected_role, allow_cross_role=allow_cross_role,
    )
    return client, matches[0]


def _same_case(document, metadata):
    remote = _metadata(document)
    return all(remote.get(key) == metadata.get(key) for key in ("producer", "case_key"))


def _ensure_tags(client, kb_id, names, *, create=True):
    path = f"/knowledge-bases/{quote_id(kb_id)}/tags"
    existing = {item.get("name"): item["id"] for item in client.records(path)}
    for name in names:
        if name in existing:
            continue
        if not create:
            raise PublishError("remote_tags_mismatch", "Required remote tags do not exist")
        try:
            created = client.request(path, method="POST", payload={"name": name}).get("data")
            if not isinstance(created, dict) or created.get("name") != name or not created.get("id"):
                raise PublishError("invalid_response", "Tag creation did not return the requested tag")
            existing[name] = created["id"]
        except RemoteHTTPError as error:
            if error.status != 409:
                raise
            refreshed = {item.get("name"): item["id"] for item in client.records(path)}
            if name not in refreshed:
                raise PublishError("tag_conflict", "Conflicting tag creation could not be reconciled") from error
            existing.update(refreshed)
    return [existing[name] for name in names]


def _apply_tags_after_upload(client, kb_id, document_id, names):
    if not names:
        return {"tagging_status": "not_requested", "tag_ids": []}
    try:
        tag_ids = _ensure_tags(client, kb_id, names)
        _verify_remote(client, kb_id, document_id, {}, tag_ids, repair_tags=True)
    except REQUEST_ERRORS as error:
        from autowork_core.knowledge.document_sync import _safe_error

        return {
            "tagging_status": "pending",
            "tagging_error": _safe_error(error, client),
        }
    return {"tagging_status": "applied", "tag_ids": tag_ids}


def _upload_file(client, kb_id, *, file_name, remote_file_name, content, metadata, tag_ids):
    boundary = "Autowork" + uuid4().hex
    fields = {
        "fileName": remote_file_name,
        "metadata": json.dumps(metadata, ensure_ascii=False),
        "enable_multimodel": "true",
        "tag_ids": ",".join(tag_ids),
    }
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        for name, value in fields.items()
    ]
    if any(character in file_name for character in '\r\n\x00"'):
        raise PublishError("invalid_identity", "Invalid local filename for multipart upload")
    parts.extend([
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file_name}"\r\nContent-Type: text/markdown\r\n\r\n'.encode("utf-8"),
        content,
        f"\r\n--{boundary}--\r\n".encode("ascii"),
    ])
    response = client.request(
        f"/knowledge-bases/{quote_id(kb_id)}/knowledge/file", method="POST",
        data=b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}",
    )
    document = response.get("data")
    if not isinstance(document, dict) or not isinstance(document.get("id"), str) or not document["id"].strip():
        raise PublishError("upload_outcome_unknown", "Upload returned no document ID; query remote state before retrying")
    return document["id"]


def _verify_remote(client, kb_id, document_id, metadata, tag_ids, *, repair_tags=True):
    document = client.document(document_id, kb_id)
    if metadata and (
        not _same_case(document, metadata)
        or _metadata(document).get("content_sha256") != metadata["content_sha256"]
    ):
        raise PublishError("identity_conflict", "Remote document identity or artifact hash differs")
    if metadata:
        remote_path = PurePosixPath(metadata["remote_file_name"])
        folder = "" if str(remote_path.parent) == "." else remote_path.parent.as_posix()
        if document.get("folder_path", "") != folder or document.get("file_name") != remote_path.name:
            raise PublishError("remote_path_mismatch", "Remote folder or filename differs from the requested destination")
        actual = client.request(f"/knowledge/{quote_id(document_id)}/download", raw=True)
        if hashlib.sha256(actual).hexdigest() != metadata["content_sha256"]:
            raise PublishError("remote_hash_mismatch", "Downloaded source bytes differ from the selected local file")
    required = set(tag_ids)
    if not required.issubset(_tag_ids(document)):
        if not repair_tags:
            raise PublishError("remote_tags_mismatch", "Required tags are missing from the remote document")
        client.request("/knowledge/tags", method="PUT", payload={"kb_id": kb_id, "updates": {document_id: sorted(_tag_ids(document) | required)}})
        document = client.document(document_id, kb_id)
        if not required.issubset(_tag_ids(document)):
            raise PublishError("remote_tags_mismatch", "Required tags were not persisted")
    return document


def _wiki_snapshot(client, kb_id, document_id):
    pages = client.wiki_pages(kb_id)
    snapshot = []
    for page in pages:
        references = {str(reference).split("|", 1)[0].strip() for reference in page.get("source_refs") or ()}
        if document_id not in references:
            continue
        slug = page.get("slug")
        detail = client.request(
            f"/knowledgebase/{quote_id(kb_id)}/wiki/pages/{urllib.parse.quote(slug, safe='/')}",
            allow_bare=True,
        )
        detail = detail.get("data", detail)
        if isinstance(detail, dict) and isinstance(detail.get("page"), dict):
            detail = detail["page"]
        if (
            not isinstance(detail, dict) or detail.get("slug") != slug
            or not detail.get("content", "").strip()
            or not isinstance(detail.get("version"), int) or detail["version"] < 1
        ):
            raise PublishError("invalid_response", "Wiki page has no readable content")
        references = {str(reference).split("|", 1)[0].strip() for reference in detail.get("source_refs") or ()}
        if document_id not in references:
            raise PublishError("wiki_changed", "Wiki source binding changed during inspection")
        snapshot.append({
            "slug": slug, "version": detail.get("version"),
            "content_sha256": hashlib.sha256(detail["content"].encode("utf-8")).hexdigest(),
        })
    return sorted(snapshot, key=lambda item: item["slug"])


def _valid_review(review, expected):
    return (
        isinstance(review, dict) and review.get("status") == "accepted"
        and all(review.get(key) == expected.get(key) for key in ("api_url", "knowledge_base_id", "document_id", "content_sha256", "pages"))
        and isinstance(review.get("checks"), dict)
        and all(review["checks"].get(name) is True for name in REVIEW_CHECKS)
        and isinstance(review.get("notes"), str) and bool(review["notes"].strip())
    )


def _review_result(client, kb_id, document, metadata):
    status = document.get("parse_status")
    if status in {"failed", "error", "cancelled", "deleting"}:
        raise PublishError("parse_failed", "The remote document did not parse successfully; previous versions were preserved")
    if status != "completed":
        return {"status": "processing", "parse_status": status or "unknown"}
    pages = _wiki_snapshot(client, kb_id, document["id"])
    if not any(page["slug"] == f"summary/{document['id']}" for page in pages) or not any(not page["slug"].startswith("summary/") for page in pages):
        return {"status": "wiki_pending", "parse_status": status}
    review = {
        "status": "pending", "api_url": client.api_url, "knowledge_base_id": kb_id,
        "document_id": document["id"], "content_sha256": metadata["content_sha256"],
        "pages": pages, "checks": {name: False for name in REVIEW_CHECKS}, "notes": "",
    }
    accepted = _metadata(document).get("wiki_acceptance")
    if accepted:
        try:
            previous = json.loads(accepted)
        except (ValueError, TypeError):
            previous = {}
        if _valid_review(previous, review):
            old_versions = json.loads(_metadata(document).get("previous_versions", "[]"))
            remaining_ids = {item["id"] for item in client.documents(kb_id)} & {item["id"] for item in old_versions}
            return {
                "status": "accepted",
                "parse_status": status, "review": previous,
                "retirement_candidates": sorted(remaining_ids),
            }
    return {"status": "wiki_review_required", "parse_status": status, "review": review}


def publish_case_markdown(
    markdown_path, *, kb_id=None, api_url=None,
    remote_file_name=None, tags=None, source_sha256=None, evidence_sha256=None,
    dry_run=False, timeout=120, resume_only=False, inspect_wiki=False,
    allow_cross_role=False,
):
    from autowork_core.knowledge.case_verifier import verify_case_markdown
    from autowork_core.knowledge.document_sync import _redact_result, _safe_error
    from config.paths import Paths
    from config.settings import settings

    path = Path(markdown_path)
    if not path.is_absolute():
        path = Paths.BASE_DIR / path
    path = path.resolve()
    content = path.read_bytes()
    identity = extract_case_metadata_from_markdown(path, payload_bytes=content)
    validation = verify_case_markdown(path)
    if validation["status"] != "passed":
        raise PublishError("invalid_document", "; ".join(validation["errors"]))
    capture_case_id = identity["capture_case_id"] or uuid5(
        NAMESPACE_URL,
        f"bdd-autowork:explicit-markdown:{path}:{identity['content_sha256']}",
    ).hex
    destination = _publication_path(
        _safe_remote_filename(remote_file_name or identity["remote_file_name"]), capture_case_id, identity["content_sha256"],
    )
    try:
        names = normalize_case_tags(
            identity["feature_code"], [*identity["tags"], *normalize_case_tags(identity["feature_code"], tags)],
        )
    except ValueError as error:
        raise PublishError("invalid_identity", str(error)) from error
    result = {
        "case_key": identity["case_key"], "feature_source": identity["feature_source"],
        "scenario_name": identity["scenario_name"], "markdown_path": str(path),
        "remote_file_name": destination, "tags": names, "case_sha256": identity["content_sha256"],
        "capture_case_id": capture_case_id,
    }
    pre_chars = (validation.get("summary") or {}).get("pre_image_chars")
    if pre_chars is not None and pre_chars > (validation.get("summary") or {}).get("pre_image_budget", 32768):
        result["warnings"] = [w for w in (validation.get("warnings") or ()) if "WeKnora" in w]
    if dry_run:
        resolved_kb = kb_id or getattr(settings, "weknora_engineering_cases_kb", "") or None
        if resolved_kb is not None and not allow_cross_role:
            resolved_kb = resolve_kb_role_selector(
                resolved_kb, expected_role="ENGINEERING_CASES", allow_cross_role=allow_cross_role,
            )
        return {**result, "status": "dry_run", "knowledge_base_id": resolved_kb, "remote_checked": False}
    client, knowledge_base = preflight_publish(
        kb_id=kb_id, api_url=api_url, timeout=timeout, allow_cross_role=allow_cross_role,
    )
    selected_kb = knowledge_base["id"]
    metadata = {
        key: str(identity[key]) for key in (
            "case_key", "feature_source", "feature_code", "scenario_name", "run_id", "content_sha256", "evidence_sha256",
        )
    }
    metadata["source_sha256"] = source_sha256 or identity["content_sha256"]
    metadata["evidence_sha256"] = evidence_sha256 or identity["evidence_sha256"] or metadata["source_sha256"]
    metadata.update(producer=PRODUCER, capture_case_id=capture_case_id, remote_file_name=destination)
    receipt = _read_receipt(capture_case_id, selected_kb, identity["content_sha256"], api_url=client.api_url)
    reused = bool(receipt)
    if receipt:
        if receipt.get("case_key", identity["case_key"]) != identity["case_key"]:
            raise PublishError("receipt_conflict", "The recorded submission belongs to another case")
        bound_destination = _safe_remote_path(receipt.get("remote_file_name") or "")
        if remote_file_name is not None and destination != bound_destination:
            raise PublishError("receipt_conflict", "An existing publication is bound to another destination; no upload or move was attempted")
        destination = bound_destination
        metadata["remote_file_name"] = destination
        result["remote_file_name"] = destination
    else:
        if resume_only:
            raise PublishError("document_not_found", "No publication receipt exists for this exact artifact; read-only status will not upload it")
        if hashlib.sha256(path.read_bytes()).hexdigest() != identity["content_sha256"]:
            raise PublishError("source_changed", "The selected file changed before upload")
        receipt = {
            "publication_version": 2, "api_url": client.api_url, "knowledge_base_id": selected_kb,
            "capture_case_id": capture_case_id, "content_sha256": identity["content_sha256"],
            "case_key": identity["case_key"], "document_id": None, "remote_file_name": destination,
            "upload_outcome": "not_sent", "verification_status": "not_attempted", "tagging_status": "not_attempted",
        }
        _save_publication(receipt)
    receipt = dict(receipt)
    result["reused_existing"] = reused
    if receipt.get("document_id") is None and receipt.get("upload_outcome") == "unknown":
        try:
            receipt["document_id"] = _reconcile_publication(receipt, client)
        except REQUEST_ERRORS as error:
            if isinstance(error, PublishError) and error.code == "publication_conflict":
                raise
            detail = _safe_error(error, client)
            if not resume_only:
                receipt["error"] = detail
                _save_publication(receipt)
            return _publication_result(result, receipt, "upload_outcome_unknown", error=detail, read_only=resume_only)
        receipt.update(upload_outcome="accepted", verification_status="pending")
        if not resume_only:
            _save_publication(receipt)
    if receipt.get("document_id") is None:
        if resume_only:
            return _publication_result(result, receipt, SUBMISSION_STATUSES[receipt["upload_outcome"]], error=receipt.get("error"), read_only=True)
        if hashlib.sha256(path.read_bytes()).hexdigest() != identity["content_sha256"]:
            raise PublishError("source_changed", "The selected file changed before upload")
        receipt.update(upload_outcome="unknown", verification_status="pending")
        _save_publication(receipt)
        try:
            try:
                document_id = _upload_file(
                    client, selected_kb, file_name=path.name, remote_file_name=destination,
                    content=content, metadata=metadata, tag_ids=[],
                )
            except RemoteHTTPError as error:
                conflict = error.payload
                duplicate = conflict.get("data") if isinstance(conflict, dict) else None
                if error.status != 409:
                    raise
                if not isinstance(conflict, dict) or conflict.get("code") != "duplicate_file" or not isinstance(duplicate, dict) or not isinstance(duplicate.get("id"), str) or not duplicate["id"].strip():
                    raise PublishError("upload_conflict", "Conflict was not a verified duplicate_file response", stage="upload") from error
                document_id = duplicate["id"]
                result["reused_existing"] = True
        except REQUEST_ERRORS as error:
            receipt["error"] = _safe_error(error, client)
            rejected = isinstance(error, RemoteHTTPError) and 400 <= error.status < 500 and error.status != 408
            rejected = rejected or isinstance(error, PublishError) and error.code == "upload_conflict"
            if rejected:
                receipt.update(upload_outcome="rejected", verification_status="not_attempted")
            _save_publication(receipt)
            if rejected:
                raise
            return _publication_result(result, receipt, "upload_outcome_unknown", error=receipt["error"])
        receipt.update(document_id=document_id, upload_outcome="accepted", verification_status="pending")
        _save_publication(receipt)
    document_id = receipt["document_id"]
    try:
        document = _verify_remote(client, selected_kb, document_id, metadata, (), repair_tags=False)
    except REQUEST_ERRORS as error:
        detail = _safe_error(error, client)
        conflict = isinstance(error, PublishError) and error.code in {
            "identity_conflict", "remote_path_mismatch", "remote_hash_mismatch", "remote_tags_mismatch",
        }
        receipt.update(verification_status="failed" if conflict else "pending", error=detail)
        if not resume_only:
            _save_publication(receipt)
        if conflict:
            raise
        return _publication_result(result, receipt, "verification_pending", error=detail, read_only=resume_only)
    receipt.update(upload_outcome="accepted", verification_status="verified")
    receipt.pop("error", None)
    if resume_only and not set(receipt.get("tag_ids") or ()).issubset(_tag_ids(document)):
        receipt["tagging_status"] = "pending"
    if not resume_only:
        _save_publication(receipt)
        receipt.pop("tagging_error", None)
        receipt.update(_apply_tags_after_upload(client, selected_kb, document_id, names))
        _save_publication(receipt)
    response = _publication_result(result, receipt, "uploaded", document=document, read_only=resume_only)
    response["knowledge_base_name"] = knowledge_base.get("name")
    if "tagging_error" in receipt:
        response["tagging_error"] = receipt["tagging_error"]
    if inspect_wiki:
        try:
            response.update(_review_result(client, selected_kb, document, metadata))
        except PublishError as error:
            if error.code != "parse_failed":
                raise
            response.update(status="processing_failed", processing_error=_safe_error(error, client))
    return _redact_result(response, client)


def accept_case_markdown(markdown_path, review_path, *, kb_id=None, api_url=None, timeout=120, checks=None, notes=None, expected_review_sha256=None):
    submitted_review = None
    if checks is not None:
        if (not isinstance(checks, dict) or set(checks) != set(REVIEW_CHECKS)
                or any(checks[name] is not True for name in REVIEW_CHECKS)
                or not isinstance(notes, str) or not notes.strip()):
            raise PublishError("invalid_review", "Explicitly pass every review check and nonempty notes", stage="review decision")
        content = Path(review_path).read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_review_sha256:
            raise PublishError("review_changed", "Review file differs from the inspected snapshot", stage="review decision")
        submitted_review = json.loads(content.decode("utf-8-sig"))
        if not isinstance(submitted_review, dict):
            raise PublishError("invalid_review", "Review must be an object", stage="review decision")
        submitted_review = {**submitted_review, "status": "accepted", "checks": dict(checks), "notes": notes}
    elif notes is not None or expected_review_sha256 is not None:
        raise PublishError("invalid_review", "Field submission requires explicit checks", stage="review decision")
    result = publish_case_markdown(markdown_path, kb_id=kb_id, api_url=api_url, timeout=timeout, resume_only=True, inspect_wiki=True)
    if result["status"] not in {"wiki_review_required", "retirement_pending", "accepted"}:
        return result
    review = submitted_review if submitted_review is not None else json.loads(Path(review_path).read_text(encoding="utf-8-sig"))
    if not _valid_review(review, result["review"]):
        raise PublishError("invalid_review", "Review must accept every check and match this server, document, file hash and current Wiki page versions")
    client, knowledge_base = preflight_publish(kb_id=kb_id, api_url=api_url, timeout=timeout)
    document_id = result["weknora_document_id"]
    selected_kb = knowledge_base["id"]
    document = client.document(document_id, selected_kb)
    metadata = _metadata(document)
    if _wiki_snapshot(client, selected_kb, document_id) != review["pages"]:
        raise PublishError("wiki_changed", "Wiki pages changed before acceptance was recorded")
    metadata["wiki_acceptance"] = json.dumps(review, ensure_ascii=False)
    client.request(f"/knowledge/{quote_id(document_id)}", method="PUT", payload={"custom_metadata": metadata})
    if _metadata(client.document(document_id, selected_kb)).get("wiki_acceptance") != metadata["wiki_acceptance"]:
        raise PublishError("invalid_response", "Wiki acceptance was not persisted")
    previous = json.loads(metadata.get("previous_versions", "[]"))
    result.update(status="accepted", retirement_candidates=[item["id"] for item in previous])
    if submitted_review is not None:
        result["review"] = submitted_review
    return result


def retire_case_versions(markdown_path, *, kb_id=None, api_url=None, timeout=120):
    """Submit retirement only after explicit caller authorization and current acceptance."""
    result = publish_case_markdown(markdown_path, kb_id=kb_id, api_url=api_url, timeout=timeout, resume_only=True, inspect_wiki=True)
    if result["status"] != "accepted":
        raise PublishError("retirement_not_allowed", "Current artifact is not accepted; no old version was deleted", stage="retire")
    client, knowledge_base = preflight_publish(kb_id=kb_id, api_url=api_url, timeout=timeout)
    selected_kb = knowledge_base["id"]
    document_id = result["weknora_document_id"]
    document = client.document(document_id, selected_kb)
    metadata = _metadata(document)
    previous = json.loads(metadata.get("previous_versions", "[]"))
    if not previous:
        return result
    known = {item["id"] for item in previous} | {document_id}
    if any(_same_case(item, metadata) and item["id"] not in known for item in client.documents(selected_kb)):
        raise PublishError("concurrent_conflict", "Another version appeared; no previous document was retired", stage="retire")
    pending = []
    tasks = json.loads(metadata.get("retirement_tasks", "{}"))
    for old in previous:
        try:
            old_document = client.document(old["id"], selected_kb)
        except RemoteHTTPError as error:
            if error.status == 404:
                continue
            raise
        if not _same_case(old_document, metadata) or _metadata(old_document).get("content_sha256") != old["content_sha256"]:
            raise PublishError("identity_conflict", "A previous version changed; retirement stopped")
        if old_document.get("parse_status") != "deleting" and old["id"] not in tasks:
            deletion = client.request(f"/knowledge/{quote_id(old['id'])}", method="DELETE").get("data")
            if not isinstance(deletion, dict) or not deletion.get("task_id"):
                raise PublishError("retirement_unknown", "Deletion was not acknowledged with a task ID")
            tasks[old["id"]] = deletion["task_id"]
        pending.append(old["id"])
    if tasks:
        metadata["retirement_tasks"] = json.dumps(tasks)
        client.request(f"/knowledge/{quote_id(document_id)}", method="PUT", payload={"custom_metadata": metadata})
    result.update(status="retirement_pending" if pending else "accepted", pending_retirements=pending)
    return result


def clean_capture_directory(capture_dir, *, kb_id=None, api_url=None, timeout=120):
    from autowork_core.knowledge.case_context import scan_captures
    from config.paths import Paths

    supplied = Path(capture_dir)
    path = supplied.resolve()
    root = (Paths.ARTIFACTS_DIR / "knowledge-captures").resolve()
    if path.parent != root or supplied.is_symlink() or not (path / "observations.json").is_file():
        raise PublishError("cleanup_refused", "Cleanup requires one explicit capture run directly under artifacts/knowledge-captures")
    observations = (path / "observations.json").read_bytes()
    if not json.loads(observations).get("finished_at"):
        raise PublishError("cleanup_refused", "An unfinished capture run cannot be cleaned")
    selection = scan_captures(path)
    if selection["skipped_failed_runs"]:
        raise PublishError("cleanup_refused", "A capture run containing failed or unreadable scenarios cannot be deleted")
    cases = selection["selected_cases"]
    if not cases:
        raise PublishError("cleanup_refused", "Capture contains no identifiable cases")
    for case in cases:
        result = publish_case_markdown(path / case["local_file_name"], kb_id=kb_id, api_url=api_url, timeout=timeout, resume_only=True, inspect_wiki=True)
        if result["status"] != "accepted":
            raise PublishError("cleanup_refused", "Every case in the run must have a current remote Wiki acceptance before whole-run cleanup")
        if result.get("retirement_candidates"):
            raise PublishError("cleanup_refused", "A case still has old versions pending explicit retirement; whole-run cleanup is blocked")
    if (path / "observations.json").read_bytes() != observations:
        raise PublishError("cleanup_refused", "Capture changed during cleanup verification")
    shutil.rmtree(path)
    return {"status": "cleaned", "capture_dir": str(path), "verified_cases": len(cases)}