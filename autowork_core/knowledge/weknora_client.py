from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request


class PublishError(RuntimeError):
    def __init__(self, code, message, *, stage=None, details=None):
        super().__init__(message)
        self.code = code
        self.stage = stage
        self.details = details or {}


class RemoteHTTPError(PublishError):
    def __init__(self, status, message, *, stage, server_message=None, payload=None):
        super().__init__(
            "http_error", message, stage=stage,
            details={
                "http_status": status,
                **({"server_message": server_message} if server_message else {}),
            },
        )
        self.status = status
        self.payload = payload


def _server_message(payload):
    if not isinstance(payload, dict):
        return None
    for key in ("message", "error", "details"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            value = re.sub(r"(?i)(authorization|x-api-key)\s*[:=]\s*\S+", r"\1: [redacted]", value.strip())
            return value[:500]
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


class WeKnoraClient:
    def __init__(self, api_url, api_key, *, timeout=120):
        address = urllib.parse.urlsplit(api_url)
        if (
            address.scheme not in {"http", "https"} or not address.netloc
            or address.username or address.password or address.query or address.fragment
            or address.path not in {"", "/"}
        ):
            raise PublishError("configuration_error", "WEKNORA_SETTING.API_URL must be an HTTP(S) server root, not an MCP/API URL")
        if not api_key or api_key.startswith(("mcp_", "Bearer ")):
            raise PublishError("configuration_error", "Configure a REST API key, not an MCP endpoint token")
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self._headers = {"Accept": "application/json", "X-API-Key": api_key}
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(self, path, *, method="GET", payload=None, raw=False, data=None, content_type=None, allow_bare=False, operation=None):
        headers = dict(self._headers)
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self.api_url + "/api/v1" + path, data=data, headers=headers, method=method)
        stage = operation or f"{method} {path.split('?', 1)[0]}"
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as error:
            raw_error = error.read()
            try:
                error_payload = json.loads(raw_error)
            except (ValueError, UnicodeDecodeError):
                error_payload = None
            raise RemoteHTTPError(
                error.code,
                f"WeKnora rejected {stage}",
                stage=stage,
                server_message=_server_message(error_payload),
                payload=error_payload,
            ) from None
        except urllib.error.URLError as error:
            reason = str(getattr(error, "reason", "connection failed"))[:300]
            raise PublishError(
                "connection_failed", f"Could not reach WeKnora during {stage}",
                stage=stage, details={"reason": reason},
            ) from None
        if raw:
            return body
        try:
            result = json.loads(body)
        except (ValueError, UnicodeDecodeError) as error:
            raise PublishError("invalid_response", f"Invalid JSON response for {stage}", stage=stage) from error
        if not isinstance(result, dict):
            raise PublishError("invalid_response", f"Unexpected response shape for {stage}", stage=stage)
        if result.get("success") is not True:
            if allow_bare and "success" not in result:
                return result
            raise PublishError("invalid_response", f"Unsuccessful response for {stage}", stage=stage, details={"server_message": _server_message(result) or ""})
        return result

    def records(self, path):
        records = []
        seen = set()
        page = 1
        stage = f"GET {path.split('?', 1)[0]}"
        while True:
            separator = "&" if "?" in path else "?"
            response = self.request(f"{path}{separator}page={page}&page_size=100")
            data = response.get("data")
            container = data if isinstance(data, dict) else response
            if isinstance(data, dict):
                data = next((data[key] for key in ("items", "pages", "tags", "knowledge_bases", "data") if isinstance(data.get(key), list)), None)
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise PublishError("invalid_response", "Expected a paginated record list", stage=stage, details={"page": page})
            identities = [item.get("id") or item.get("slug") for item in data]
            if any(not identity or identity in seen for identity in identities) or len(set(identities)) != len(identities):
                raise PublishError("invalid_response", "Repeated or unidentified records in pagination", stage=stage, details={"page": page})
            seen.update(identities)
            records.extend(data)
            total = container.get("total", response.get("total"))
            if total is not None:
                if not isinstance(total, int) or total < len(records) or (not data and len(records) < total):
                    raise PublishError("invalid_response", "Inconsistent pagination total", stage=stage, details={"page": page})
                if len(records) == total:
                    return records
            elif len(data) < 100:
                return records
            page += 1

    def document(self, document_id, kb_id):
        document = self.request(f"/knowledge/{quote_id(document_id)}").get("data")
        if not isinstance(document, dict) or document.get("id") != document_id or document.get("knowledge_base_id") != kb_id:
            raise PublishError("identity_conflict", "Document ID or knowledge-base binding does not match")
        return document

    def documents(self, kb_id):
        return self.records(f"/knowledge-bases/{quote_id(kb_id)}/knowledge")

    def wiki_pages(self, kb_id):
        pages = []
        seen = set()
        page = 1
        while True:
            response = self.request(
                f"/knowledgebase/{quote_id(kb_id)}/wiki/pages?page={page}&page_size=100",
                allow_bare=True,
            )
            data = response.get("data", response)
            if isinstance(data, dict):
                records = data.get("pages")
                total = data.get("total")
            else:
                records = None
                total = None
            if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
                raise PublishError("invalid_response", "Expected a paginated Wiki page list")
            slugs = [item.get("slug") for item in records]
            if any(not slug or slug in seen for slug in slugs):
                raise PublishError("invalid_response", "Repeated or unidentified Wiki pages in pagination")
            seen.update(slugs)
            pages.extend(records)
            if isinstance(total, int) and len(pages) == total:
                return pages
            if total is None and len(records) < 100:
                return pages
            if not isinstance(total, int) or total < len(pages):
                raise PublishError("invalid_response", "Inconsistent Wiki page pagination")
            page += 1


def quote_id(value):
    return urllib.parse.quote(str(value), safe="")


KB_ROLES = ("FRAMEWORK_DOCS", "ENGINEERING_CASES", "PRODUCT_DOCS", "STATIC_UI_DOCS")


def _configured_kb_roles():
    from config.settings import settings

    role_map = {
        "FRAMEWORK_DOCS": str(getattr(settings, "weknora_framework_docs_kb", "") or "").strip(),
        "ENGINEERING_CASES": str(getattr(settings, "weknora_engineering_cases_kb", "") or "").strip(),
        "PRODUCT_DOCS": str(getattr(settings, "weknora_product_docs_kb", "") or "").strip(),
        "STATIC_UI_DOCS": str(getattr(settings, "weknora_static_ui_docs_kb", "") or "").strip(),
    }
    non_empty = [val for val in role_map.values() if val]
    if len(non_empty) != len(set(non_empty)):
        raise PublishError(
            "configuration_error",
            "WEKNORA_SETTING.KNOWLEDGE_BASES roles must not point to the same knowledge base",
            stage="preflight",
        )
    return role_map


def resolve_kb_role_selector(
    kb_selector: str | None,
    *,
    expected_role: str | None = None,
    allow_cross_role: bool = False,
) -> str:
    role_map = _configured_kb_roles()
    selector = str(kb_selector or "").strip()
    if not selector and expected_role:
        selector = role_map.get(expected_role, "")
    if not selector:
        role_hint = (
            f" or configure WEKNORA_SETTING.KNOWLEDGE_BASES.{expected_role} in Bdd/config.yaml"
            if expected_role else ""
        )
        raise PublishError(
            "configuration_error",
            f"Provide an explicit target knowledge base with --kb{role_hint}",
            stage="preflight",
        )
    if expected_role and not allow_cross_role:
        for role, configured in role_map.items():
            if role != expected_role and configured and selector == configured:
                raise PublishError(
                    "knowledge_base_role_mismatch",
                    f"Target knowledge base {selector!r} is bound to role {role}, not {expected_role}",
                    stage="preflight",
                    details={"target": selector, "conflict_role": role, "expected_role": expected_role},
                )
    return selector


def verify_resolved_kb_role(
    kb_record: dict,
    *,
    expected_role: str | None = None,
    allow_cross_role: bool = False,
) -> None:
    if not expected_role or allow_cross_role:
        return
    role_map = _configured_kb_roles()
    target_identities = {val for val in (kb_record.get("id"), kb_record.get("name")) if val}
    for role, configured in role_map.items():
        if role != expected_role and configured and configured in target_identities:
            raise PublishError(
                "knowledge_base_role_mismatch",
                f"Target knowledge base {kb_record.get('name') or kb_record.get('id')!r} is bound to role {role}, not {expected_role}",
                stage="preflight",
                details={
                    "target": kb_record.get("name") or kb_record.get("id"),
                    "conflict_role": role,
                    "expected_role": expected_role,
                },
            )
