from __future__ import annotations

import ast
import hashlib
import inspect
import json
import re
from pathlib import Path, PurePosixPath

from autowork_core.page import BasePage
from autowork_core.utils.debug_tools.recorder.ai_capability_registry import (
    capability_by_name,
)
from autowork_core.utils.debug_tools.recorder.identity import (
    locator_candidate_id as expected_locator_candidate_id,
)


IMPLEMENTATION_MANIFEST_VERSION = "1.12"
IMPLEMENTATION_PACKET_VERSION = "1.2"


def compact_implementation_manifest_contract():
    return {
        "implementation_manifest_version": IMPLEMENTATION_MANIFEST_VERSION,
        "purpose": (
            "Deterministic post-Plan edit task. AI implements bodies but "
            "cannot expand file scope, mutate reuse assets, or replace Plan."
        ),
        "identity": [
            "request_id",
            "plan_id",
            "plan_fingerprint",
            "generation_input_snapshot_fingerprint",
            "implementation_manifest_id",
            "implementation_manifest_fingerprint",
        ],
        "edit_scope": {
            "writable": "allowed_changes",
            "ai_writable": "ai_editable_changes",
            "system_owned": "system_owned_changes",
            "immutable": "read_only_reuse",
            "protected": "protected_paths",
        },
        "tasks": {
            "files": "create/modify/reuse strategy with baseline hash",
            "steps": (
                "Feature order, exact Gherkin decorator/pattern/function "
                "parameters, behavior file, table use, operations, and "
                "strict locator/dynamic input bindings"
            ),
            "methods": (
                "receiver, create definition signature or frozen candidate "
                "signature, and per-call-group argument sources"
            ),
            "locator_patch": (
                "window/view/action-bound YAML ensure patches"
            ),
            "pic_templates": (
                "authorized exact files under Bdd/data/recorder_pic"
            ),
            "package_markers": "empty_or_docstring_only policy",
        },
        "rules": [
            "Prepare derives the Manifest only from validated Plan, Brief, and input snapshot.",
            "Finish rebuilds the Manifest and rejects identity or content drift.",
            "Actual changes must be a subset of allowed_changes; read_only_reuse is immutable.",
            "Transaction project guard rejects unreported changes outside generation roots; runtime artifacts and large bundled models are excluded from the guard.",
            "Prepare rejects symbolic links in generation roots or guarded project paths; snapshots never follow link targets.",
            "Every Step exposes its exact decorator keyword, template pattern, and function parameters; Scenario Outline inputs bind to those parameters.",
            "Every named operation target exposes one strict $loc: runtime reference; bare locator names are not implementation arguments.",
            "Window Root locators preserve only criteria explicitly frozen in root_criteria; observed runtime titles are evidence and are never promoted automatically into title/title_re locator constraints.",
            "A top-level Root patch may enrich an existing system-owned mapping only when every existing field already equals the frozen patch; ordinary locator patches remain exact ensure operations.",
            "Text-content read, removal, and assertion locators omit dynamic name/title values while retaining frozen structural identity such as AutoId, control type, and Root; materialization may only remove those two fields from an otherwise identical mapping.",
            "A read-only locator key requires a content-addressed locator/window-root candidate; Page method string references do not prove YAML key existence.",
            "Exact Page method reuse binds the frozen linear call sequence and verifies the Step method call plus its frozen arguments; modify/create remain writable body implementations.",
            "Manifest does not generate implementation bodies or replace Plan-to-Code validation.",
        ],
    }


def build_implementation_manifest(
        plan_artifact,
        brief,
        generation_input_snapshot,
        *,
        request_id,
        allowed_write_roots,
        protected_write_roots,
        protected_root_files,
    ):
    plan_artifact = plan_artifact if isinstance(plan_artifact, dict) else {}
    plan = plan_artifact.get("plan") or {}
    brief = brief if isinstance(brief, dict) else {}
    snapshot = (
        generation_input_snapshot
        if isinstance(generation_input_snapshot, dict)
        else {}
    )
    baseline = snapshot.get("files") or {}
    errors = []
    files = {}
    methods = {}
    locators = {}
    steps = []
    owners = plan.get("window_owners") or {}
    actions = {
        (str(item.get("step_id") or ""), str(item.get("id") or "")): item
        for item in brief.get("actions") or ()
        if item.get("id")
    }

    for owner_id, owner in owners.items():
        owner = owner if isinstance(owner, dict) else {}
        resolution = owner.get("resolution") or {}
        owner_write = resolution.get("strategy") == "create_new"
        _add_file(
            files,
            owner.get("page_object"),
            "window_page",
            write_required=owner_write,
            baseline=baseline,
            errors=errors,
        )
        _add_file(
            files,
            owner.get("root_locator_file"),
            "window_locators",
            write_required=owner_write,
            baseline=baseline,
            errors=errors,
        )
        for view_id, view in (owner.get("views") or {}).items():
            view = view if isinstance(view, dict) else {}
            view_object = _safe_path(view.get("view_object"))
            view_locator_file = _safe_path(view.get("locator_file"))
            _add_file(
                files,
                view_object,
                f"view:{view_id}",
                write_required=owner_write or (
                    bool(view_object) and view_object not in baseline
                ),
                baseline=baseline,
                errors=errors,
            )
            _add_file(
                files,
                view_locator_file,
                f"view_locators:{view_id}",
                write_required=owner_write or (
                    bool(view_locator_file) and view_locator_file not in baseline
                ),
                baseline=baseline,
                errors=errors,
            )

    for step_order, (step_id, step) in enumerate(
        (plan.get("steps") or {}).items(),
        start=1,
    ):
        step_id = str(step_id)
        step = step if isinstance(step, dict) else {}
        behavior = step.get("behavior_resolution") or {}
        behavior_write = behavior.get("strategy") != "reuse"
        behavior_file = _add_file(
            files,
            step.get("behavior_file"),
            f"step:{step_id}",
            write_required=behavior_write,
            baseline=baseline,
            errors=errors,
        )
        _add_file(
            files,
            step.get("data_file"),
            f"data:{step_id}",
            write_required=bool(step.get("data_file")),
            baseline=baseline,
            errors=errors,
        )
        operation_tasks = []
        locator_tasks = []
        method_segment = 0
        previous_method = None
        for order, operation in enumerate(
                step.get("operations") or (),
                start=1,
        ):
            if not isinstance(operation, dict):
                continue
            owner_id = str(operation.get("window_owner") or "")
            owner = owners.get(owner_id) or {}
            view_owner = str(operation.get("view_owner") or "") or None
            implementation_location = str(
                operation.get("implementation_location") or ""
            )
            implementation_method = str(
                operation.get("implementation_method") or ""
            ) or None
            implementation_path = _implementation_path(
                owner,
                view_owner,
            )
            resolution = operation.get("implementation_resolution") or {}
            method_write = (
                implementation_location == "page_method"
                and resolution.get("strategy") in {"create", "modify"}
            )
            if implementation_location == "page_method":
                if implementation_method != previous_method:
                    method_segment += 1
                    previous_method = implementation_method
                _add_file(
                    files,
                    implementation_path,
                    f"method:{implementation_method}",
                    write_required=method_write,
                    baseline=baseline,
                    errors=errors,
                )
                _add_method(
                    methods,
                    operation,
                    implementation_path,
                    step_id,
                    order,
                    method_segment,
                    errors,
                )
            operation_tasks.append({
                "order": order,
                "operation": str(operation.get("op") or ""),
                "target": operation.get("target"),
                "target_binding": _operation_target_binding(operation),
                "runtime_api": f"BasePage.{operation.get('op')}",
                "runtime_signature": _runtime_signature(
                    operation.get("op")
                ),
                "receiver": _receiver_contract(
                    operation,
                    owner,
                    view_owner,
                ),
                "implementation_location": implementation_location,
                "implementation_method": implementation_method,
                "parameters": dict(operation.get("parameters") or {}),
                "value": operation.get("value"),
                "value_source": operation.get("source"),
                "value_provenance": dict(
                    operation.get("value_provenance") or {}
                ),
                "input_binding": _operation_input_binding(operation),
                "result_binding": operation.get("result_binding"),
                "target_action_id": operation.get("target_action_id"),
            })

        locator_file = step.get("locator_file")
        for locator in step.get("locators") or ():
            if not isinstance(locator, dict):
                continue
            route, route_error = _locator_route(
                locator,
                step,
                owners,
                actions,
                step_id,
            )
            if route_error:
                errors.append(route_error)
                continue
            owner = route["owner"]
            routed_locator_file = route.get("locator_file") or locator_file
            if not routed_locator_file:
                errors.append(
                    f"Step {step_id} locator {locator.get('name')}缺少owner文件"
                )
                continue
            evidence_name = str(
                locator.get("evidence_name")
                or locator.get("name")
                or ""
            )
            locator_write = bool(
                (owner.get("resolution") or {}).get("strategy")
                == "create_new"
                or str(routed_locator_file) not in baseline
                or not _frozen_locator_exists(
                    brief,
                    str(routed_locator_file),
                    str(locator.get("name") or ""),
                )
            )
            _add_file(
                files,
                routed_locator_file,
                f"locators:{step_id}",
                write_required=locator_write,
                baseline=baseline,
                errors=errors,
            )
            patch = _locator_patch(
                locator,
                owner,
                brief,
                step_id,
                actions,
                route.get("action_ids") or [],
                route.get("operations") or [],
            )
            task = {
                "file": str(routed_locator_file),
                "key": str(
                    route.get("root_locator")
                    if str(locator.get("kind") or "") == "top_level"
                    else locator.get("name")
                    or ""
                ),
                "kind": str(locator.get("kind") or ""),
                "evidence_name": locator.get("evidence_name"),
                "window_owner": route.get("owner_id"),
                "view_owner": route.get("view_owner"),
                "target_action_ids": route.get("action_ids") or [],
                "operation": (
                    "ensure_or_enrich"
                    if str(locator.get("kind") or "") == "top_level"
                    else "ensure_or_refine_content"
                    if _has_text_content_operation(
                        route.get("operations") or []
                    )
                    else "ensure"
                ),
                "patch": patch,
            }
            locator_tasks.append(task)
            locators[(task["file"], task["key"])] = task

        step_contract = _step_contract(brief, step_id)
        steps.append({
            "order": step_order,
            "step_id": step_id,
            "gherkin_text": step_contract["text"],
            "gherkin_pattern": step_contract["pattern"],
            "gherkin_arguments": step_contract["arguments"],
            "step_definition": {
                "decorator": step_contract["decorator"],
                "pattern": step_contract["pattern"],
                "python_decorator": (
                    f"@{step_contract['decorator']}("
                    f"{json.dumps(step_contract['pattern'], ensure_ascii=False)}"
                    ")"
                ),
                "function_parameters": [
                    "context",
                    *(
                        item["parameter"]
                        for item in step_contract["arguments"]
                    ),
                ],
            },
            "behavior": {
                "path": behavior_file,
                "owner": step.get("behavior_owner"),
                "strategy": behavior.get("strategy"),
                "candidate_id": behavior.get("candidate_id"),
                "symbol": behavior.get("symbol"),
                "step_decorator": behavior.get("step_decorator"),
                "step_pattern": behavior.get("step_pattern"),
            },
            "page_object": step.get("page_object"),
            "locator_file": locator_file,
            "table_usage": step.get("table_usage"),
            "operations": operation_tasks,
            "locator_patch": locator_tasks,
            "unresolved_issues": [
                dict(item)
                for item in step.get("unresolved_issues") or ()
                if isinstance(item, dict)
            ],
        })

    for authorization in plan.get("pic_authorizations") or ():
        if not isinstance(authorization, dict) or not authorization.get(
                "authorized"
        ):
            continue
        target_path = _pic_target_path(
            authorization.get("target_data_path")
        )
        if target_path is None:
            errors.append(
                "Implementation Manifest PIC target_data_path无效: "
                f"{authorization.get('target_data_path')}"
            )
            continue
        _add_file(
            files,
            target_path,
            f"pic_template:{authorization.get('authorization_id')}",
            write_required=True,
            baseline=baseline,
            errors=errors,
        )
        step_id = str(authorization.get("step_id") or "")
        step = (plan.get("steps") or {}).get(step_id) or {}
        locator_file = _add_file(
            files,
            step.get("locator_file"),
            f"pic_locators:{authorization.get('authorization_id')}",
            write_required=True,
            baseline=baseline,
            errors=errors,
        )
        action_id = str(authorization.get("action_id") or "")
        locator_name = _pic_operation_target(step, action_id)
        region_name = str(
            authorization.get("region_locator_name") or ""
        )
        region_patch = authorization.get("region_locator")
        threshold = (
            authorization.get("cross_frame_validation") or {}
        ).get("threshold")
        target_data_path = str(
            authorization.get("target_data_path") or ""
        ).replace("\\", "/").removeprefix("Bdd/data/")
        if not all((
            locator_file,
            locator_name,
            region_name,
            isinstance(region_patch, dict),
            region_patch,
            target_data_path,
            threshold is not None,
        )):
            errors.append(
                "Implementation Manifest PIC locator task无效: "
                f"{authorization.get('authorization_id')}"
            )
            continue
        for task in ({
            "file": locator_file,
            "key": region_name,
            "kind": "region",
            "evidence_name": region_name,
            "window_owner": None,
            "view_owner": None,
            "target_action_ids": [authorization.get("action_id")],
            "operation": "ensure",
            "patch": dict(region_patch),
        }, {
            "file": locator_file,
            "key": locator_name,
            "kind": "pic",
            "evidence_name": locator_name,
            "window_owner": None,
            "view_owner": None,
            "target_action_ids": [authorization.get("action_id")],
            "operation": "ensure",
            "patch": {
                "by": "pic",
                "file": target_data_path,
                "region": region_name,
                "threshold": threshold,
            },
        }):
            existing = locators.get((task["file"], task["key"]))
            if existing is not None and existing != task:
                errors.append(
                    "Implementation Manifest PIC locator冲突: "
                    f"{task['file']}:{task['key']}"
                )
                continue
            locators[(task["file"], task["key"])] = task

    package_markers = _package_markers(files, baseline)
    for marker in package_markers:
        _add_file(
            files,
            marker["path"],
            "package_marker",
            write_required=marker["strategy"] == "create",
            baseline=baseline,
            errors=errors,
        )
    _finalize_methods(methods, brief, errors)
    file_values = sorted(files.values(), key=lambda item: item["path"])
    system_owned_changes = sorted({
        marker["path"]
        for marker in package_markers
        if marker.get("strategy") == "create"
    } | {
        item["file"]
        for item in locators.values()
        if item.get("file") in files
        and files[item["file"]].get("strategy") in {"create", "modify"}
    })
    allowed_changes = [
        item["path"]
        for item in file_values
        if item["strategy"] in {"create", "modify"}
    ]
    manifest = {
        "implementation_manifest_version": IMPLEMENTATION_MANIFEST_VERSION,
        "request_id": str(request_id or ""),
        "plan_id": str(plan_artifact.get("plan_id") or ""),
        "plan_fingerprint": str(
            plan_artifact.get("plan_fingerprint") or ""
        ),
        "generation_input_snapshot_fingerprint": _fingerprint(snapshot),
        "status": "ready" if not errors else "failed",
        "allowed_write_roots": sorted(
            str(item).replace("\\", "/") for item in allowed_write_roots
        ),
        "protected_paths": {
            "roots": sorted(
                str(item).replace("\\", "/")
                for item in protected_write_roots
            ),
            "files": sorted(
                str(item).replace("\\", "/")
                for item in protected_root_files
            ),
        },
        "allowed_changes": allowed_changes,
        "ai_editable_changes": sorted(
            set(allowed_changes) - set(system_owned_changes)
        ),
        "system_owned_changes": system_owned_changes,
        "read_only_reuse": [
            item["path"]
            for item in file_values
            if item["strategy"] == "reuse"
        ],
        "files": file_values,
        "steps": steps,
        "methods": sorted(
            methods.values(),
            key=lambda item: (item["path"], item["symbol"]),
        ),
        "locator_patch": sorted(
            locators.values(),
            key=lambda item: (item["file"], item["key"]),
        ),
        "package_markers": package_markers,
        "errors": errors,
    }
    fingerprint = implementation_manifest_fingerprint(manifest)
    manifest["implementation_manifest_id"] = (
        "implementation-manifest-" + fingerprint[:16]
    )
    manifest["implementation_manifest_fingerprint"] = fingerprint
    return manifest


def build_implementation_packet(manifest):
    """Project deterministic implementation syntax without adding facts."""
    if not implementation_manifest_identity_is_valid(manifest):
        raise ValueError("Implementation Packet要求有效Manifest")
    files = {
        str(item.get("path") or ""): item
        for item in manifest.get("files") or ()
        if isinstance(item, dict) and item.get("path")
    }
    pages = _packet_pages(manifest, files)
    steps = []
    for step in manifest.get("steps") or ():
        page_path = _packet_step_page_path(step)
        page = pages.get(page_path)
        operations = []
        for operation in step.get("operations") or ():
            receiver = operation.get("receiver") or {}
            operation_page = pages.get(
                _packet_operation_page_path(step, operation)
            )
            operations.append({
                "order": operation.get("order"),
                "operation": operation.get("operation"),
                "receiver": (
                    operation_page.get("receiver")
                    if receiver.get("kind") == "step_bound_page"
                    and operation_page
                    else receiver.get("receiver")
                ),
                "receiver_expression": _packet_receiver_expression(
                    page,
                    operation_page,
                    receiver,
                ),
                "view_owner": receiver.get("view_owner"),
                "page": operation_page,
                "target": (operation.get("target_binding") or {}).get(
                    "reference"
                ),
                "parameters": operation.get("parameters") or {},
                "value": operation.get("value"),
                "value_source": operation.get("value_source"),
                "implementation_location": operation.get(
                    "implementation_location"
                ),
                "implementation_method": operation.get(
                    "implementation_method"
                ),
            })
        definition = step.get("step_definition") or {}
        steps.append({
            "order": step.get("order"),
            "step_id": step.get("step_id"),
            "path": (step.get("behavior") or {}).get("path"),
            "python_decorator": definition.get("python_decorator"),
            "function_parameters": definition.get("function_parameters") or [],
            "page": page,
            "operations": operations,
            "unresolved_issues": list(
                step.get("unresolved_issues") or ()
            ),
            "issue_template": (
                {
                    "import": (
                        "from autowork_core.runtime.generation_issue import "
                        "unresolved_generation_issue"
                    ),
                    "call": "unresolved_generation_issue",
                    "arguments": [
                        {
                            "issue_id": issue.get("issue_id"),
                            "step_id": issue.get("step_id"),
                            "issue_type": issue.get("issue_type"),
                        }
                        for issue in step.get("unresolved_issues") or ()
                    ],
                }
                if step.get("unresolved_issues")
                else None
            ),
        })
    return {
        "implementation_packet_version": IMPLEMENTATION_PACKET_VERSION,
        "derived_from": {
            "implementation_manifest_id": manifest[
                "implementation_manifest_id"
            ],
            "implementation_manifest_fingerprint": manifest[
                "implementation_manifest_fingerprint"
            ],
        },
        "ai_editable_changes": list(manifest.get("ai_editable_changes") or ()),
        "system_owned_changes": list(
            manifest.get("system_owned_changes") or ()
        ),
        "pages": sorted(pages.values(), key=lambda item: item["path"]),
        "steps": steps,
        "methods": list(manifest.get("methods") or ()),
        "rule": (
            "This packet is a deterministic Manifest projection. It does not "
            "authorize files, operations, targets, values, or business choices "
            "beyond the bound Manifest."
        ),
    }


def _packet_receiver_expression(step_page, operation_page, receiver):
    if receiver.get("kind") != "step_bound_page" or operation_page is None:
        return receiver.get("receiver")
    if receiver.get("view_owner"):
        parent = (operation_page or {}).get("parent_receiver") or (
            step_page or {}
        ).get("receiver")
        if parent:
            return f"{parent}.{operation_page.get('receiver')}"
    return operation_page.get("receiver")


def _packet_module_name(path):
    value = str(path or "").replace("\\", "/")
    if value.endswith(".py"):
        value = value[:-3]
    return value.replace("/", ".")


def _packet_page_class_name(path):
    parts = str(path or "").replace("\\", "/").split("/")
    package = parts[-2] if len(parts) >= 2 else "generated"
    return "".join(part.capitalize() for part in package.split("_")) + "Page"


def _packet_view_class_name(path):
    name = Path(str(path or "")).stem
    return "".join(part.capitalize() for part in name.split("_")) + "View"


def _packet_receiver_name(path):
    parts = str(path or "").replace("\\", "/").split("/")
    return _identifier(parts[-2] if len(parts) >= 2 else "page")


def _packet_view_receiver_name(path):
    return _identifier(Path(str(path or "")).stem or "view")


def _packet_step_page_path(step):
    direct = str((step or {}).get("page_object") or "")
    if direct:
        return direct
    for operation in (step or {}).get("operations") or ():
        receiver = (operation or {}).get("receiver") or {}
        if receiver.get("kind") == "step_bound_page":
            value = str(receiver.get("page_object") or "")
            if value:
                return value
    return ""


def _packet_operation_page_path(step, operation):
    receiver = (operation or {}).get("receiver") or {}
    if receiver.get("kind") == "step_bound_page":
        page_path = str(receiver.get("page_object") or "")
        if page_path:
            return page_path
    return _packet_step_page_path(step)


def _packet_pages(manifest, files):
    page_paths = set()
    for step in manifest.get("steps") or ():
        page_paths.add(_packet_step_page_path(step))
        page_paths.update(
            _packet_operation_page_path(step, operation)
            for operation in step.get("operations") or ()
        )
    view_files = _packet_view_files(files, manifest)
    top_level_patches = {
        str(item.get("file") or ""): item.get("key")
        for item in manifest.get("locator_patch") or ()
        if item.get("kind") == "top_level"
    }
    result = {}
    for page_path in sorted(path for path in page_paths if path):
        view = view_files.get(page_path)
        if view is not None:
            locator_file = view.get("locator_file")
            parent_path = _packet_parent_page_path(page_path)
            result[page_path] = {
                "path": page_path,
                "module": _packet_module_name(page_path),
                "class_name": _packet_view_class_name(page_path),
                "receiver": _packet_view_receiver_name(page_path),
                "base_class": "WindowView",
                "parent_path": parent_path,
                "parent_receiver": _packet_receiver_name(parent_path),
                "locator_file": locator_file,
                "active_locator": top_level_patches.get(locator_file),
                "root_locator": top_level_patches.get(locator_file),
                "strategy": (files.get(page_path) or {}).get("strategy"),
            }
            continue
        locator_file = _packet_locator_file_for_page(page_path)
        result[page_path] = {
            "path": page_path,
            "module": _packet_module_name(page_path),
            "class_name": _packet_page_class_name(page_path),
            "receiver": _packet_receiver_name(page_path),
            "base_class": "WindowPage",
            "root_locator_file": locator_file,
            "root_locator": top_level_patches.get(locator_file),
            "strategy": (files.get(page_path) or {}).get("strategy"),
        }
    for page in result.values():
        if page.get("base_class") != "WindowView":
            continue
        parent = result.get(page.get("parent_path"))
        if parent is None:
            continue
        parent.setdefault("views", []).append({
            "receiver": page.get("receiver"),
            "class_name": page.get("class_name"),
            "path": page.get("path"),
            "locator_file": page.get("locator_file"),
            "active_locator": page.get("active_locator"),
            "root_locator": page.get("root_locator"),
        })
    return result


def _packet_parent_page_path(view_path):
    parts = str(view_path or "").replace("\\", "/").split("/")
    if len(parts) < 2:
        return ""
    return "/".join([*parts[:-1], "page.py"])


def _packet_view_files(files, manifest):
    locator_by_view = {}
    for path, file_record in files.items():
        for role in file_record.get("roles") or ():
            role = str(role)
            if role.startswith("view_locators:"):
                locator_by_view[role.split(":", 1)[1]] = path
    result = {}
    for path, file_record in files.items():
        for role in file_record.get("roles") or ():
            role = str(role)
            if role.startswith("view:"):
                view_id = role.split(":", 1)[1]
                result[path] = {
                    "view_id": view_id,
                    "locator_file": locator_by_view.get(view_id),
                }
    if result:
        return result
    view_paths = {
        str(((operation.get("receiver") or {}).get("page_object")) or "")
        for step in manifest.get("steps") or ()
        for operation in step.get("operations") or ()
        if (operation.get("receiver") or {}).get("view_owner")
    }
    return {path: {"view_id": Path(path).stem} for path in view_paths if path}


def _packet_locator_file_for_page(page_path):
    package = str(page_path).replace("\\", "/").split("/")[-2]
    return f"Bdd/locators/{package}/window.yaml"


def _pic_operation_target(step, action_id):
    matches = [
        str(operation.get("target") or "")
        for operation in (step or {}).get("operations") or ()
        if isinstance(operation, dict)
        and str(action_id) in {
            str(value)
            for value in operation.get("action_ids") or ()
        }
        and operation.get("target")
    ]
    return matches[0] if len(matches) == 1 else ""


def implementation_manifest_fingerprint(manifest):
    value = {
        key: item
        for key, item in dict(manifest or {}).items()
        if key not in {
            "implementation_manifest_id",
            "implementation_manifest_fingerprint",
        }
    }
    return _fingerprint(value)


def implementation_manifest_identity_is_valid(manifest):
    if not isinstance(manifest, dict):
        return False
    fingerprint = implementation_manifest_fingerprint(manifest)
    return all((
        manifest.get("implementation_manifest_version")
        == IMPLEMENTATION_MANIFEST_VERSION,
        bool(manifest.get("request_id")),
        bool(manifest.get("plan_id")),
        bool(manifest.get("plan_fingerprint")),
        manifest.get("implementation_manifest_fingerprint") == fingerprint,
        manifest.get("implementation_manifest_id")
        == "implementation-manifest-" + fingerprint[:16],
    ))


def implementation_manifest_matches_transaction(
        manifest,
        plan_artifact,
        brief,
        generation_input_snapshot,
        *,
        request_id,
        allowed_write_roots,
        protected_write_roots,
        protected_root_files,
    ):
    if not implementation_manifest_identity_is_valid(manifest):
        return False
    expected = build_implementation_manifest(
        plan_artifact,
        brief,
        generation_input_snapshot,
        request_id=request_id,
        allowed_write_roots=allowed_write_roots,
        protected_write_roots=protected_write_roots,
        protected_root_files=protected_root_files,
    )
    return manifest == expected


def _add_file(
        files,
        path,
        role,
        *,
        write_required,
        baseline,
        errors,
    ):
    raw_path = path
    path = _safe_path(raw_path)
    if path is None:
        if raw_path not in (None, ""):
            errors.append(f"Implementation Manifest路径无效: {raw_path}")
        return None
    record = files.setdefault(path, {
        "path": path,
        "roles": [],
        "strategy": "reuse",
        "baseline_sha256": (
            (baseline.get(path) or {}).get("sha256")
        ),
    })
    if (baseline.get(path) or {}).get("is_symlink") is True:
        errors.append(f"Implementation Manifest拒绝符号链接: {path}")
    if role not in record["roles"]:
        record["roles"].append(role)
        record["roles"].sort()
    if write_required:
        record["strategy"] = "modify" if path in baseline else "create"
    return path


def _add_method(
        methods,
        operation,
        path,
        step_id,
        order,
        segment,
        errors,
    ):
    symbol = str(operation.get("implementation_method") or "")
    if not symbol or not path:
        return
    resolution = operation.get("implementation_resolution") or {}
    record = methods.setdefault(symbol, {
        "symbol": symbol,
        "path": path,
        "receiver": "self",
        "strategy": resolution.get("strategy"),
        "candidate_id": resolution.get("candidate_id"),
        "definition_signature": None,
        "call_groups": [],
    })
    if any((
        record["path"] != path,
        record["strategy"] != resolution.get("strategy"),
        record["candidate_id"] != resolution.get("candidate_id"),
    )):
        errors.append(
            f"Implementation Manifest method {symbol}身份不一致"
        )
        return
    group = next((
        item
        for item in record["call_groups"]
        if item["step_id"] == step_id and item["segment"] == segment
    ), None)
    if group is None:
        group = {
            "step_id": step_id,
            "segment": segment,
            "inputs": [],
            "operations": [],
        }
        record["call_groups"].append(group)
    input_value = _required_input(operation)
    if input_value and input_value not in group["inputs"]:
        group["inputs"].append(input_value)
    group["operations"].append({
        "step_id": step_id,
        "order": order,
        "operation": operation.get("op"),
        "input": input_value,
    })


def _finalize_methods(methods, brief, errors):
    for record in methods.values():
        groups = record["call_groups"]
        arities = {len(group["inputs"]) for group in groups}
        if len(arities) > 1:
            errors.append(
                "Implementation Manifest method "
                f"{record['symbol']}调用参数数量不一致: {sorted(arities)}"
            )
        parameter_count = next(iter(arities), 0)
        parameter_names = _parameter_names(
            groups[0]["inputs"] if groups else []
        )
        candidate = _implementation_candidate(
            brief,
            record.get("candidate_id"),
        )
        candidate_parameters = _candidate_parameters(candidate)
        if record["strategy"] != "create" and candidate_parameters:
            if len(candidate_parameters) != parameter_count:
                errors.append(
                    "Implementation Manifest method "
                    f"{record['symbol']}冻结签名参数数量不一致: "
                    f"expected={len(candidate_parameters)} "
                    f"actual={parameter_count}"
                )
            else:
                parameter_names = candidate_parameters
        for group in groups:
            group["arguments"] = [
                {
                    **item,
                    "parameter": parameter_names[index],
                }
                for index, item in enumerate(group["inputs"])
                if index < len(parameter_names)
            ]
        if record["strategy"] == "create":
            record["definition_signature"] = {
                "source": "compiled_call_groups",
                "receiver": "self",
                "parameters": parameter_names,
                "python": _python_signature(
                    record["symbol"],
                    parameter_names,
                ),
            }
            continue
        record["definition_signature"] = {
            "source": "frozen_candidate",
            "candidate_id": record.get("candidate_id"),
            "python": (candidate or {}).get("signature"),
            "required_call_arity": parameter_count,
        }


def _parameter_names(inputs):
    result = []
    seen = set()
    for index, item in enumerate(inputs, start=1):
        name = _identifier((item or {}).get("suggested_name"))
        if name in seen:
            name = f"{name}_{index}"
        while name in seen:
            name += "_value"
        result.append(name)
        seen.add(name)
    return result


def _required_input(operation):
    source = str(operation.get("source") or "")
    if source.startswith(("examples.", "table.", "runtime.")):
        return {
            "source": source,
            "suggested_name": _identifier(source.split(".", 1)[1]),
        }
    capability = capability_by_name(str(operation.get("op") or ""))
    if capability is None or capability.value_argument is None:
        return None
    provenance = operation.get("value_provenance") or {}
    kind = str(provenance.get("kind") or "")
    if source in {"recorded_action", "literal"} and kind:
        return {
            "source": (
                f"{kind}.{provenance.get('reference')}"
                if provenance.get("reference")
                else kind
            ),
            "value": operation.get("value"),
            "suggested_name": "value",
        }
    return None


def _candidate_parameters(candidate):
    signature = str((candidate or {}).get("signature") or "").strip()
    if not signature:
        return []
    try:
        tree = ast.parse(signature + ":\n    pass\n")
    except (SyntaxError, ValueError):
        return []
    function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ),
        None,
    )
    if function is None:
        return []
    names = [
        argument.arg
        for argument in (
            list(function.args.posonlyargs)
            + list(function.args.args)
            + list(function.args.kwonlyargs)
        )
    ]
    return names[1:] if names and names[0] in {"self", "cls"} else names


def _frozen_locator_exists(brief, path, name):
    if not path or not name:
        return False
    candidates = list(
        (brief.get("semantics") or {}).get("reuse_candidates") or ()
    )
    for window in (brief.get("window_ownership") or {}).get("windows") or ():
        for owner in (window.get("owner_match") or {}).get("candidates") or ():
            if str(owner.get("root_locator_file") or "") == path:
                if str(owner.get("root_locator") or "") == name:
                    return True
                candidates.extend(owner.get("method_candidates") or ())
    return any(
        str(candidate.get("kind") or "") in {"locator", "window_root"}
        and str(candidate.get("path") or "") == path
        and name in {
            str(reference)
            for reference in candidate.get("references") or ()
        }
        for candidate in candidates
        if isinstance(candidate, dict)
    )


def _operation_input_binding(operation):
    value = _required_input(operation)
    if value is None:
        return None
    source = value["source"]
    capability = capability_by_name(str(operation.get("op") or ""))
    value_argument = (
        capability.value_argument
        if capability is not None
        else None
    )
    return {
        "source": source,
        "step_parameter": (
            value["suggested_name"]
            if source.startswith("examples.")
            else None
        ),
        "runtime_argument_index": (
            value_argument[0] if value_argument is not None else None
        ),
        "runtime_parameter": (
            value_argument[1] if value_argument is not None else None
        ),
    }


def _operation_target_binding(operation):
    target = str(operation.get("target") or "").strip()
    if not target:
        return None
    capability = capability_by_name(str(operation.get("op") or ""))
    runtime_parameter = None
    if capability is not None and capability.ast_match_profile == "ocr_assertion":
        runtime_parameter = "region"
    else:
        method = getattr(BasePage, str(operation.get("op") or ""), None)
        if callable(method):
            parameters = list(inspect.signature(method).parameters)
            runtime_parameter = next((
                name for name in parameters if name != "self"
            ), None)
    return {
        "kind": "named_locator",
        "reference": f"$loc:{target}",
        "runtime_parameter": runtime_parameter,
    }


def _receiver_contract(operation, owner, view_owner):
    location = str(operation.get("implementation_location") or "")
    if location == "page_method":
        return {
            "kind": "declared_method",
            "symbol": operation.get("implementation_method"),
            "receiver": "self",
        }
    return {
        "kind": "step_bound_page",
        "page_object": (
            ((owner.get("views") or {}).get(view_owner) or {}).get(
                "view_object"
            )
            if view_owner
            else owner.get("page_object")
        ),
        "view_owner": view_owner,
    }


def _implementation_path(owner, view_owner):
    if view_owner:
        return ((owner.get("views") or {}).get(view_owner) or {}).get(
            "view_object"
        )
    return owner.get("page_object")


def _locator_route(locator, step, owners, actions, step_id):
    kind = str(locator.get("kind") or "")
    name = str(locator.get("name") or "")
    evidence_name = str(locator.get("evidence_name") or name)
    routes = {}
    if kind == "top_level":
        for owner_id, owner in owners.items():
            if str((owner or {}).get("root_locator") or "") not in {
                name,
                evidence_name,
            }:
                for view_id, view in (owner.get("views") or {}).items():
                    if str(view.get("active_locator") or "") not in {
                        name,
                        evidence_name,
                    }:
                        continue
                    operations = [
                        item
                        for item in step.get("operations") or ()
                        if isinstance(item, dict)
                        and str(item.get("window_owner") or "") == str(owner_id)
                        and str(item.get("view_owner") or "") == str(view_id)
                    ]
                    routes[(str(owner_id), str(view_id))] = _locator_route_value(
                        owner_id,
                        owner,
                        str(view_id),
                        operations,
                    )
                continue
            operations = [
                item
                for item in step.get("operations") or ()
                if isinstance(item, dict)
                and str(item.get("window_owner") or "") == str(owner_id)
                and not str(item.get("view_owner") or "")
            ]
            routes[(str(owner_id), None)] = _locator_route_value(
                owner_id,
                owner,
                None,
                operations,
            )
    else:
        action_ids = {
            str(action_id)
            for (action_step_id, action_id), action in actions.items()
            if action_step_id == str(step_id)
            and str((action.get("target") or {}).get("locator_name") or "")
            == evidence_name
        }
        for operation in step.get("operations") or ():
            if not isinstance(operation, dict):
                continue
            if str(operation.get("target_action_id") or "") not in action_ids:
                continue
            owner_id = str(operation.get("window_owner") or "")
            owner = owners.get(owner_id) or {}
            view_owner = str(operation.get("view_owner") or "") or None
            key = (owner_id, view_owner)
            route = routes.setdefault(
                key,
                _locator_route_value(
                    owner_id,
                    owner,
                    view_owner,
                    [],
                ),
            )
            route["operations"].append(operation)
            route["action_ids"].append(
                str(operation.get("target_action_id") or "")
            )
    if not routes:
        operation_owners = {
            (
                str(item.get("window_owner") or ""),
                str(item.get("view_owner") or "") or None,
            )
            for item in step.get("operations") or ()
            if isinstance(item, dict) and item.get("window_owner")
        }
        if len(operation_owners) == 1:
            owner_id, view_owner = next(iter(operation_owners))
            owner = owners.get(owner_id) or {}
            routes[(owner_id, view_owner)] = _locator_route_value(
                owner_id,
                owner,
                view_owner,
                [
                    item
                    for item in step.get("operations") or ()
                    if isinstance(item, dict)
                ],
            )
    if len(routes) != 1:
        return None, (
            f"Step {step_id} locator {name}无法唯一绑定window/view: "
            f"{sorted(str(key) for key in routes)}"
        )
    route = next(iter(routes.values()))
    route["action_ids"] = sorted(set(route["action_ids"]))
    return route, None


def _locator_route_value(owner_id, owner, view_owner, operations):
    view = ((owner or {}).get("views") or {}).get(view_owner) or {}
    return {
        "owner_id": str(owner_id),
        "owner": owner or {},
        "view_owner": view_owner,
        "locator_file": (
            view.get("locator_file")
            if view_owner
            else (owner or {}).get("root_locator_file")
        ),
        "root_locator": (
            view.get("active_locator")
            if view_owner
            else (owner or {}).get("root_locator")
        ),
        "operations": list(operations),
        "action_ids": [
            str(item.get("target_action_id") or "")
            for item in operations
            if item.get("target_action_id")
        ],
    }


def _locator_patch(
        locator,
        owner,
        brief,
        step_id,
        actions,
        routed_action_ids,
    routed_operations,
    ):
    kind = str(locator.get("kind") or "")
    evidence_name = str(
        locator.get("evidence_name") or locator.get("name") or ""
    )
    if kind == "top_level":
        root_name = str(
            locator.get("evidence_name")
            or owner.get("evidence_root")
            or owner.get("root_locator")
            or evidence_name
        )
        window = next((
            item
            for item in (brief.get("window_ownership") or {}).get(
                "windows"
            ) or ()
            if str(item.get("root_name") or "") == root_name
        ), {})
        patch = _window_root_patch(window)
        patch["top_level"] = True
        return patch
    action = next((
        action
        for (action_step_id, action_id), action in actions.items()
        if action_step_id == str(step_id)
        and action_id in set(routed_action_ids)
        and str((action.get("target") or {}).get("locator_name") or "")
        == evidence_name
    ), {})
    target = action.get("target") or {}
    candidate_id = str(locator.get("locator_candidate_id") or "")
    target_locator = target.get("locator") or {}
    if (
        "locator_candidates" in target
        and target_locator.get("by", "child") in {"child", "xpath"}
        and not candidate_id
    ):
        raise ValueError(
            "Implementation Manifest当前结构locator缺少candidate ID"
        )
    if candidate_id:
        candidates = [
            item
            for item in target.get("locator_candidates") or ()
            if isinstance(item, dict)
            and str(item.get("candidate_id") or "") == candidate_id
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"Implementation Manifest引用未知或冲突locator "
                f"candidate: {candidate_id}"
            )
        candidate = candidates[0]
        validation = candidate.get("validation") or {}
        if any((
            candidate_id != expected_locator_candidate_id(
                candidate.get("locator") or {},
                candidate.get("reason"),
            ),
            validation.get("status") != "unique",
            validation.get("target_matches") is not True,
        )):
            raise ValueError(
                f"Implementation Manifest locator candidate未验证: "
                f"{candidate_id}"
            )
        patch = dict(candidate.get("locator") or {})
    else:
        patch = dict(target.get("locator") or {})
    if not patch:
        for key in (
            "control_type",
            "auto_id",
            "name",
            "class_name",
            "title",
        ):
            if target.get(key) not in (None, ""):
                patch[key] = target[key]
    if _locator_content_is_observed(target, routed_operations):
        patch.pop("name", None)
        patch.pop("title", None)
    route_roots = {
        str(item.get("view_owner") or "")
        for item in routed_operations or ()
        if item.get("view_owner")
    }
    if len(route_roots) == 1:
        view = (owner.get("views") or {}).get(next(iter(route_roots))) or {}
        if view.get("active_locator") and "root" not in patch:
            patch["root"] = view["active_locator"]
    elif owner.get("root_locator") and "root" not in patch:
        patch["root"] = owner["root_locator"]
    return patch


def _window_root_patch(window):
    return dict((window or {}).get("root_criteria") or {})


def _locator_content_is_observed(target, operations):
    control_type = str(
        target.get("control_type") or ""
    ).casefold()
    if control_type in {"edit", "document"}:
        return _has_text_content_operation(operations)
    if control_type not in {"text", "static"}:
        return False
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        name = str(operation.get("op") or "")
        if name in {"save_text", "assert_text_empty"}:
            return True
        if name not in {
            "assert_text_equal",
            "assert_text_contains",
            "assert_text_not_contains",
        }:
            continue
        source = str(operation.get("source") or "")
        provenance = operation.get("value_provenance") or {}
        if (
            source.startswith((
                "table.",
                "examples.",
                "runtime.",
                "observed_property.",
            ))
            or provenance.get("kind") in {
                "data_table",
                "examples",
                "runtime",
            }
        ):
            return True
    return False


def _has_text_content_operation(operations):
    return any(
        isinstance(operation, dict)
        and str(operation.get("op") or "") in {
            "save_text",
            "remove_text",
            "assert_text_empty",
            "assert_text_equal",
            "assert_text_contains",
            "assert_text_not_contains",
        }
        for operation in operations or ()
    )


def _package_markers(files, baseline):
    markers = {}
    for record in files.values():
        path = PurePosixPath(record["path"])
        if (
            len(path.parts) < 4
            or path.parts[:2] != ("Bdd", "page_obj")
            or path.suffix != ".py"
            or path.name == "__init__.py"
        ):
            continue
        marker = (path.parent / "__init__.py").as_posix()
        markers[marker] = {
            "path": marker,
            "strategy": "reuse" if marker in baseline else "create",
            "policy": "empty_or_docstring_only",
        }
    return [markers[key] for key in sorted(markers)]


def _step_text(brief, step_id):
    return next((
        str(item.get("text") or "")
        for item in (brief.get("target") or {}).get("steps") or ()
        if str(item.get("id") or "") == str(step_id)
    ), "")


def _step_contract(brief, step_id):
    text = _step_text(brief, step_id)
    target = (brief or {}).get("target") or {}
    scenario = target.get("scenario") or {}
    target_step = next((
        item
        for item in target.get("steps") or ()
        if str(item.get("id") or "") == str(step_id)
    ), {})
    decorator = str(
        target_step.get("semantic_type")
        or target_step.get("keyword")
        or "step"
    ).strip().casefold()
    if decorator not in {"given", "when", "then", "step"}:
        decorator = "step"
    example_values = scenario.get("example_values") or {}
    concrete = " ".join(text.split())
    specification = scenario.get("specification") or (
        (
            ((brief or {}).get("scenario_intelligence") or {}).get(
                "specification"
            )
            or {}
        ).get("scenario")
        or {}
    ).get("specification") or {}
    template_steps = (
        (specification.get("template") or {}).get(
            "steps"
        )
        or ()
    )
    for template_step in template_steps:
        template = str((template_step or {}).get("text") or "")
        names = list(dict.fromkeys(
            match.group(1)
            for match in re.finditer(r"<([^>]+)>", template)
            if match.group(1) in example_values
        ))
        rendered = re.sub(
            r"<([^>]+)>",
            lambda match: str(
                example_values.get(match.group(1), match.group(0))
            ),
            template,
        )
        if " ".join(rendered.split()) != concrete:
            continue
        return {
            "text": text,
            "pattern": re.sub(r"<([^>]+)>", r"{\1}", template),
            "decorator": decorator,
            "arguments": [
                {
                    "parameter": _identifier(name),
                    "source": f"examples.{name}",
                }
                for name in names
            ],
        }
    return {
        "text": text,
        "pattern": text,
        "decorator": decorator,
        "arguments": [],
    }


def _runtime_signature(operation):
    method = getattr(BasePage, str(operation or ""), None)
    return str(inspect.signature(method)) if callable(method) else None


def _safe_path(value):
    if not value:
        return None
    text = str(value).replace("\\", "/").strip()
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        return None
    return path.as_posix()


def _pic_target_path(value):
    text = str(value or "").replace("\\", "/").strip()
    if re.match(r"^[A-Za-z]:", text):
        return None
    path = PurePosixPath(text)
    if path.is_absolute() or ".." in path.parts:
        return None
    if path.parts[:2] == ("Bdd", "data"):
        path = PurePosixPath(*path.parts[2:])
    if len(path.parts) < 2 or path.parts[0] != "recorder_pic":
        return None
    return (PurePosixPath("Bdd/data") / path).as_posix()


def _identifier(value):
    value = re.sub(r"[^0-9A-Za-z_]+", "_", str(value)).strip("_")
    if not value:
        return "value"
    if value[0].isdigit():
        value = "value_" + value
    return value


def _python_signature(symbol, parameter_names):
    method_name = str(symbol or "").rsplit(".", 1)[-1] or "method"
    parameters = ["self", *parameter_names]
    return f"def {method_name}({', '.join(parameters)})"


def _implementation_candidate(brief, candidate_id):
    candidate_id = str(candidate_id or "")
    candidates = list(
        (brief.get("semantics") or {}).get("reuse_candidates") or ()
    )
    for window in (brief.get("window_ownership") or {}).get("windows") or ():
        for owner in (window.get("owner_match") or {}).get("candidates") or ():
            candidates.extend(owner.get("method_candidates") or ())
    return next((
        item
        for item in candidates
        if str(item.get("candidate_id") or "") == candidate_id
    ), None)


def _fingerprint(value):
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "IMPLEMENTATION_MANIFEST_VERSION",
    "IMPLEMENTATION_PACKET_VERSION",
    "build_implementation_packet",
    "build_implementation_manifest",
    "compact_implementation_manifest_contract",
    "implementation_manifest_fingerprint",
    "implementation_manifest_identity_is_valid",
    "implementation_manifest_matches_transaction",
]