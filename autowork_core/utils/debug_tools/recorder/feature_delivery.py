from __future__ import annotations

import hashlib
import difflib
import json
import os
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath

from behave.parser import Parser

from autowork_core.utils.debug_tools.recorder.catalog import (
    load_recording_catalog,
)
from autowork_core.utils.debug_tools.recorder.bundle_validator import (
    validate_ai_bundle,
)
from autowork_core.utils.debug_tools.recorder.feature_plan import (
    load_feature_plan,
)
from autowork_core.utils.debug_tools.recorder.identity import (
    display_segment,
    identity_suffix,
    persistent_feature_id,
)
from autowork_core.utils.debug_tools.recorder.models import public_dict
from autowork_core.utils.debug_tools.recorder.recording_portability import (
    RecordingPackageError,
    export_recording_runs,
    import_recording_package,
    inspect_recording_package_manifests,
    validate_exportable_recording_run,
)
from autowork_core.utils.debug_tools.recorder.scope_binding import (
    recording_business_fingerprint,
)
from config.paths import Paths


FEATURE_DELIVERY_VERSION = "1.1"
FEATURE_DELIVERY_BATCH_VERSION = "1.0"
FEATURE_DELIVERY_MANIFEST = "feature-delivery.json"
FEATURE_MEMBER = "feature/source.feature"
EVIDENCE_MEMBER = "evidence/recording-package.zip"
MAX_FEATURE_BYTES = 10 * 1024 * 1024
DELIVERY_INDEX_VERSION = "1.0"
DELIVERY_INDEX_PATH = Path(".portability/feature-deliveries.json")
_DELIVERY_INDEX_LOCK = threading.RLock()


class FeatureDeliveryError(RuntimeError):
    pass


def is_feature_delivery_package(package_path):
    try:
        with zipfile.ZipFile(Path(package_path).resolve(), "r") as archive:
            return FEATURE_DELIVERY_MANIFEST in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def load_feature_delivery_index(recording_root):
    path = Path(recording_root).resolve() / DELIVERY_INDEX_PATH
    if not path.is_file():
        return {
            "feature_delivery_index_version": DELIVERY_INDEX_VERSION,
            "updated_at": None,
            "features": {},
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FeatureDeliveryError(
            f"Feature交付索引无法读取: {type(error).__name__}: {error}"
        ) from error
    if not isinstance(value, dict) or not isinstance(
            value.get("features"),
            dict,
    ):
        raise FeatureDeliveryError("Feature交付索引格式无效")
    version = value.get("feature_delivery_index_version")
    if version != DELIVERY_INDEX_VERSION:
        raise FeatureDeliveryError("Feature交付索引格式无效")
    return value


def record_feature_delivery(recording_root, kind, result):
    records = record_feature_deliveries(
        recording_root,
        kind,
        [result],
    )
    return next(iter(records.values()))


def record_feature_deliveries(recording_root, kind, results):
    if kind not in {"export", "import"}:
        raise ValueError(f"Feature交付记录类型无效: {kind}")
    prepared = []
    for result in results:
        result = dict(result or {})
        feature = dict(result.get("feature") or {})
        feature_id = str(feature.get("id") or "")
        if not feature_id:
            raise FeatureDeliveryError("Feature交付结果缺少Feature ID")
        prepared.append((feature_id, feature, result))
    if not prepared:
        return {}
    recording_root = Path(recording_root).resolve()
    path = recording_root / DELIVERY_INDEX_PATH
    with _DELIVERY_INDEX_LOCK:
        index = load_feature_delivery_index(recording_root)
        finished_at = datetime.now().isoformat(timespec="seconds")
        for feature_id, feature, result in prepared:
            record = _normalized_delivery_record(
                index["features"].get(feature_id),
                feature_id,
            )
            record[f"last_{kind}"] = {
                "delivery_id": result.get("delivery_id"),
                "package_name": Path(
                    str(result.get("package_path") or "")
                ).name,
                "package_sha256": result.get("package_sha256"),
                "source_hash": feature.get("source_hash"),
                "finished_at": finished_at,
                "runs": [
                    {
                        "session_id": str(item.get("session_id") or ""),
                        "updated_at": str(item.get("updated_at") or ""),
                    }
                    for item in (result.get("runs") or ())
                ],
            }
            index["features"][feature_id] = record
        index["updated_at"] = finished_at
        _write_json_atomic(path, index)
    return {
        feature_id: index["features"][feature_id]
        for feature_id, _feature, _result in prepared
    }


def _normalized_delivery_record(value, feature_id):
    value = dict(value or {}) if isinstance(value, dict) else {}
    result = {
        "feature_id": feature_id,
        "last_export": (
            dict(value.get("last_export"))
            if isinstance(value.get("last_export"), dict)
            else None
        ),
        "last_import": (
            dict(value.get("last_import"))
            if isinstance(value.get("last_import"), dict)
            else None
        ),
    }
    return result


def export_feature_delivery(
    feature_path,
    recording_root,
    output,
    *,
    scenario_ids=None,
):
    recording_root = Path(recording_root).resolve()
    output = _safe_delivery_output_path(output)
    plan = _load_delivery_feature_plan(feature_path)
    feature_path = plan.source_path
    source_bytes = feature_path.read_bytes()
    selected_scenario_ids = (
        frozenset(str(value) for value in scenario_ids)
        if scenario_ids is not None
        else None
    )
    if selected_scenario_ids is not None:
        known_ids = {scenario.id for scenario in plan.scenarios}
        if not selected_scenario_ids:
            raise FeatureDeliveryError("场景录制资料导出范围不能为空")
        unknown = sorted(selected_scenario_ids - known_ids)
        if unknown:
            raise FeatureDeliveryError(
                f"场景录制资料导出包含未知Scenario: {unknown}"
            )
    runs = _selected_feature_runs(
        plan,
        recording_root,
        scenario_ids=selected_scenario_ids,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output = _safe_delivery_output_path(output)
    with tempfile.TemporaryDirectory(
            prefix=".feature-delivery-",
            dir=output.parent,
    ) as value:
        evidence_path = Path(value) / "recording-package.zip"
        evidence = export_recording_runs(
            recording_root,
            [item["path"] for item in runs],
            evidence_path,
        )
        evidence_sha256 = _sha256(evidence_path)
        delivery = {
            "feature_delivery_version": FEATURE_DELIVERY_VERSION,
            "delivery_id": "feature-delivery-" + uuid.uuid4().hex,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "feature": {
                "id": plan.id,
                "name": plan.name,
                "source_relpath": plan.source_relpath,
                "source_hash": plan.source_hash,
                "sha256": hashlib.sha256(source_bytes).hexdigest(),
                "size": len(source_bytes),
                "scenario_count": len(plan.scenarios),
                "recorded_scenario_count": len(runs),
                "complete_scenario_count": sum(
                    bool(item["scenario_recording_complete"])
                    for item in runs
                ),
                "partial_scenario_count": sum(
                    not item["scenario_recording_complete"]
                    for item in runs
                ),
            },
            "runs": [
                {
                    "session_id": item["session_id"],
                    "scenario_id": item["scenario_id"],
                    "scenario_name": item["scenario_name"],
                    "example_id": item["example_id"],
                    "updated_at": item["updated_at"],
                    "recorded_step_count": item["recorded_step_count"],
                    "missing_step_count": item["missing_step_count"],
                    "total_step_count": item["total_step_count"],
                    "scenario_recording_complete": item[
                        "scenario_recording_complete"
                    ],
                }
                for item in runs
            ],
            "evidence": {
                "member": EVIDENCE_MEMBER,
                "package_id": evidence["package_id"],
                "sha256": evidence_sha256,
                "size": evidence_path.stat().st_size,
                "run_count": evidence["run_count"],
            },
            "policy": {
                "feature_included": True,
                "generation_state_included": False,
                "partial_recording_allowed": True,
                "all_scenarios_recorded": bool(
                    len(runs) == len(plan.scenarios)
                    and all(
                        item["scenario_recording_complete"]
                        for item in runs
                    )
                ),
                "export_scope": (
                    "feature"
                    if selected_scenario_ids is None
                    else "scenarios"
                ),
            },
        }
        _validate_delivery_run_progress(delivery["runs"], evidence_path)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        try:
            with zipfile.ZipFile(
                    temporary,
                    "w",
                    compression=zipfile.ZIP_DEFLATED,
                    compresslevel=6,
                    allowZip64=True,
            ) as archive:
                archive.writestr(
                    FEATURE_DELIVERY_MANIFEST,
                    json.dumps(delivery, ensure_ascii=False, indent=2),
                )
                archive.writestr(FEATURE_MEMBER, source_bytes)
                archive.write(evidence_path, EVIDENCE_MEMBER)
            _publish_without_overwrite(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    return {
        "feature_delivery_version": FEATURE_DELIVERY_VERSION,
        "delivery_id": delivery["delivery_id"],
        "package_path": str(output),
        "package_sha256": _sha256(output),
        "package_size": output.stat().st_size,
        "feature": delivery["feature"],
        "run_count": len(delivery["runs"]),
        "runs": delivery["runs"],
    }


def export_feature_deliveries(feature_paths, recording_root, output_dir):
    feature_paths = tuple(
        dict.fromkeys(Path(path).absolute() for path in feature_paths)
    )
    if not feature_paths:
        raise FeatureDeliveryError("至少选择一个Feature")
    recording_root = Path(recording_root).resolve()
    output_dir = _safe_delivery_output_path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = _safe_delivery_output_path(output_dir)
    plans = [
        _load_delivery_feature_plan(path)
        for path in feature_paths
    ]
    feature_ids = [plan.id for plan in plans]
    if len(feature_ids) != len(set(feature_ids)):
        raise FeatureDeliveryError("批量导出的Feature持久ID重复")
    targets = [
        output_dir / (
            f"{display_segment(path.stem, 48, 'feature')}-"
            f"{identity_suffix(plan.id, length=12)}.delivery.zip"
        )
        for path, plan in zip(feature_paths, plans)
    ]
    if len(targets) != len(set(targets)):
        raise FeatureDeliveryError("批量导出的目标文件名重复")
    existing = [target for target in targets if os.path.lexists(target)]
    if existing:
        raise FeatureDeliveryError(
            "目标交付包已存在，禁止覆盖: "
            + ", ".join(path.name for path in existing)
        )

    published = []
    with tempfile.TemporaryDirectory(
            prefix=".feature-delivery-batch-",
            dir=output_dir,
    ) as staging_value:
        staging = Path(staging_value)
        results = []
        for index, (feature_path, target) in enumerate(
                zip(feature_paths, targets),
                start=1,
        ):
            staged = staging / f"{index:04d}.zip"
            result = export_feature_delivery(
                feature_path,
                recording_root,
                staged,
            )
            results.append((result, staged, target))
        try:
            for _result, staged, target in results:
                _publish_without_overwrite(staged, target)
                published.append(target)
        except Exception as error:
            cleanup_errors = []
            for target in reversed(published):
                try:
                    target.unlink(missing_ok=True)
                except OSError as cleanup_error:
                    cleanup_errors.append(
                        f"{target.name}: {type(cleanup_error).__name__}: "
                        f"{cleanup_error}"
                    )
            if cleanup_errors:
                raise FeatureDeliveryError(
                    f"批量交付发布失败且回滚不完整: {error}; "
                    + "; ".join(cleanup_errors)
                ) from error
            raise

    packages = []
    for result, _staged, target in results:
        packages.append({
            **result,
            "package_path": str(target),
        })
    batch_fingerprint = hashlib.sha256(json.dumps(
        [
            {
                "feature_id": item["feature"]["id"],
                "package_name": Path(item["package_path"]).name,
                "package_sha256": item["package_sha256"],
            }
            for item in packages
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "feature_delivery_batch_version": FEATURE_DELIVERY_BATCH_VERSION,
        "package_path": str(output_dir),
        "package_sha256": batch_fingerprint,
        "package_count": len(packages),
        "run_count": sum(item["run_count"] for item in packages),
        "packages": tuple(packages),
    }


def preview_feature_delivery(
    package_path,
    project_root,
    *,
    target_feature_path=None,
):
    package_path = Path(package_path).resolve()
    project_root = Path(project_root).resolve()
    if not package_path.is_file():
        raise FileNotFoundError(f"Feature交付包不存在: {package_path}")
    with zipfile.ZipFile(package_path, "r") as archive:
        infos = {info.filename: info for info in archive.infolist()}
        expected = {
            FEATURE_DELIVERY_MANIFEST,
            FEATURE_MEMBER,
            EVIDENCE_MEMBER,
        }
        if set(infos) != expected:
            raise FeatureDeliveryError("Feature交付包文件清单无效")
        if any(_zip_info_is_symlink(info) for info in infos.values()):
            raise FeatureDeliveryError("Feature交付包不能包含符号链接")
        if infos[FEATURE_MEMBER].file_size > MAX_FEATURE_BYTES:
            raise FeatureDeliveryError("Feature文件超过安全上限")
        try:
            delivery = json.loads(
                archive.read(FEATURE_DELIVERY_MANIFEST).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FeatureDeliveryError("Feature交付清单无法解析") from error
        _validate_delivery_manifest(delivery)
        source_bytes = archive.read(FEATURE_MEMBER)
        feature = delivery["feature"]
        if any((
            len(source_bytes) != int(feature["size"]),
            hashlib.sha256(source_bytes).hexdigest() != feature["sha256"],
        )):
            raise FeatureDeliveryError("Feature文件与交付清单不一致")
        source_text = source_bytes.decode("utf-8-sig")
        Parser().parse(source_text, filename=FEATURE_MEMBER)
        if persistent_feature_id(source_text) != feature["id"]:
            raise FeatureDeliveryError("Feature持久ID与交付清单不一致")
        normalized_source = source_text.replace("\r\n", "\n").replace(
            "\r",
            "\n",
        )
        if hashlib.sha256(normalized_source.encode("utf-8")).hexdigest() != (
                feature["source_hash"]):
            raise FeatureDeliveryError("Feature规范文本哈希与交付清单不一致")
        evidence = delivery["evidence"]
        evidence_info = infos[EVIDENCE_MEMBER]
        if evidence_info.file_size != int(evidence["size"]):
            raise FeatureDeliveryError("内层录屏包大小与交付清单不一致")
        with tempfile.TemporaryDirectory(
                prefix=".feature-delivery-preview-",
        ) as staging_value:
            evidence_path = Path(staging_value) / "recording-package.zip"
            digest = hashlib.sha256()
            with archive.open(evidence_info, "r") as stream, \
                    evidence_path.open("wb") as output:
                while chunk := stream.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
            if digest.hexdigest() != evidence["sha256"]:
                raise FeatureDeliveryError(
                    "内层录屏包SHA-256与交付清单不一致"
                )
            _validate_delivery_run_progress(
                delivery["runs"],
                evidence_path,
            )

    source_relpath = _safe_project_relative(feature["source_relpath"])
    target = None
    conflict_summary = None
    matches = _project_features_with_id(project_root, feature["id"])
    if len(matches) > 1:
        target_status = "conflict"
        conflict_summary = "本地存在多个相同Feature ID的文件：" + "，".join(
            path.relative_to(project_root).as_posix()
            for path in matches
        )
    elif len(matches) == 1:
        target = matches[0]
        source_relpath = target.relative_to(project_root).as_posix()
        if target.read_bytes() == source_bytes:
            target_status = "reuse"
        else:
            target_status = "conflict"
            conflict_summary = (
                "本地同ID Feature与录制包中的Feature内容不同。"
                "请先确认Feature差异后再导入。\n"
                + _feature_conflict_summary(target.read_bytes(), source_bytes)
            )
    elif target_feature_path is None:
        target_status = "needs_target"
        target = _project_feature_target(project_root, source_relpath)
    else:
        target = _selected_feature_target(project_root, target_feature_path)
        source_relpath = target.relative_to(project_root).as_posix()
        if os.path.lexists(target):
            target_status = "conflict"
            conflict_summary = "目标位置已存在文件，请选择新的Feature保存位置。"
        else:
            target_status = "create"
    return {
        "feature_delivery_version": FEATURE_DELIVERY_VERSION,
        "delivery_id": delivery["delivery_id"],
        "package_path": str(package_path),
        "package_sha256": _sha256(package_path),
        "package_size": package_path.stat().st_size,
        "feature": feature,
        "source_relpath": (
            source_relpath.as_posix()
            if isinstance(source_relpath, Path)
            else str(source_relpath)
        ),
        "target_path": str(target) if target is not None else "",
        "target_status": target_status,
        "conflict_summary": conflict_summary,
        "run_count": len(delivery["runs"]),
        "runs": tuple(delivery["runs"]),
        "evidence": dict(delivery["evidence"]),
    }


def import_feature_delivery(
    package_path,
    project_root,
    recording_root=None,
    *,
    target_feature_path=None,
):
    package_path = Path(package_path).resolve()
    project_root = Path(project_root).resolve()
    recording_root = Path(
        recording_root
        or project_root / "artifacts" / "recording_sessions"
    ).resolve()
    try:
        recording_root.relative_to(project_root)
    except ValueError as error:
        raise FeatureDeliveryError("录屏根目录必须位于目标项目内") from error
    preview = preview_feature_delivery(
        package_path,
        project_root,
        target_feature_path=target_feature_path,
    )
    if preview["target_status"] == "needs_target":
        raise FeatureDeliveryError(
            "本地没有同ID Feature，请选择Feature保存位置后再导入"
        )
    if preview["target_status"] == "conflict":
        raise FeatureDeliveryError(
            preview.get("conflict_summary")
            or "目标Feature存在冲突，不能导入"
        )
    target = Path(preview["target_path"])
    installed = False
    with tempfile.TemporaryDirectory(prefix="feature-delivery-import-") as value:
        staging = Path(value)
        evidence_path = staging / "recording-package.zip"
        with zipfile.ZipFile(package_path, "r") as archive:
            source_bytes = archive.read(FEATURE_MEMBER)
            with archive.open(EVIDENCE_MEMBER, "r") as source, \
                    evidence_path.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
        if _sha256(evidence_path) != preview["evidence"]["sha256"]:
            raise FeatureDeliveryError("内层录屏包在导入前发生变化")
        try:
            if preview["target_status"] == "create":
                _install_feature(target, source_bytes, project_root)
                installed = True
            imported = import_recording_package(
                evidence_path,
                recording_root,
                source_relpath_override=preview["source_relpath"],
            )
        except Exception:
            if installed:
                target.unlink(missing_ok=True)
                _remove_empty_parents(target.parent, project_root)
            raise
    return {
        **preview,
        "feature_install_status": (
            "created" if installed else "reused"
        ),
        "recording_root": str(recording_root),
        "imported_run_count": int(imported.get("run_count") or 0),
        "imported_runs": tuple(imported.get("runs") or ()),
    }


def _selected_feature_runs(plan, recording_root, *, scenario_ids=None):
    catalog = load_recording_catalog(recording_root)
    entries = [
        item
        for item in catalog.get("sessions") or ()
        if isinstance(item, dict)
        and str((item.get("feature") or {}).get("id") or "") == plan.id
    ]
    selected = []
    for scenario in plan.scenarios:
        if scenario_ids is not None and scenario.id not in scenario_ids:
            continue
        expected_steps = {step.id for step in scenario.steps}
        expected_fingerprint = recording_business_fingerprint(
            public_dict(plan),
            public_dict(scenario),
        )
        candidates = []
        for entry in entries:
            if str((entry.get("scenario") or {}).get("id") or "") != scenario.id:
                continue
            run_path = exportable_feature_recording_run(
                entry,
                recording_root,
                expected_step_ids=expected_steps,
                expected_business_fingerprint=expected_fingerprint,
            )
            if run_path is None:
                continue
            candidates.append((entry, run_path))
        candidates.sort(
            key=lambda item: str(item[0].get("updated_at") or ""),
            reverse=True,
        )
        if not candidates:
            continue
        entry, run_path = candidates[0]
        manifest = _read_run_manifest(run_path)
        progress = _run_manifest_step_progress(manifest)
        selected.append({
            "session_id": str(manifest.get("session_id") or ""),
            "scenario_id": scenario.id,
            "scenario_name": scenario.name,
            "example_id": scenario.example_id,
            "updated_at": str(manifest.get("updated_at") or ""),
            "recorded_step_count": progress["recorded_step_count"],
            "missing_step_count": progress["missing_step_count"],
            "total_step_count": progress["total_step_count"],
            "scenario_recording_complete": progress[
                "scenario_recording_complete"
            ],
            "path": run_path,
        })
    if not selected:
        raise FeatureDeliveryError("Feature没有可导出的当前有效录制")
    return selected


def exportable_feature_recording_run(
        entry,
        recording_root,
        *,
        expected_step_ids,
        expected_business_fingerprint,
    ):
    expected_steps = {str(item) for item in expected_step_ids if item}
    recording_root = Path(recording_root).resolve()
    try:
        relative = _safe_run_relative(entry.get("path"))
        run_path = (recording_root / relative).resolve()
        run_path.relative_to(recording_root)
        manifest = _read_run_manifest(run_path)
        validate_exportable_recording_run(run_path, manifest)
        readiness = validate_ai_bundle(run_path)
        progress = _run_manifest_step_progress(manifest)
        if any((
            str(manifest.get("session_id") or "")
            != str(entry.get("session_id") or ""),
            readiness.get("bundle_valid") is not True,
            readiness.get("semantic_ready") is not True,
            progress["scenario_step_ids"] != expected_steps,
            not progress["declared_step_ids"],
            not progress["declared_step_ids"] <= expected_steps,
            not progress["completed_step_ids"],
            progress["completed_step_ids"]
            != progress["completed_with_take_ids"],
            (
                progress["scenario_recording_complete"]
                and readiness.get("recording_complete") is not True
            ),
        )):
            return None
        recorded_fingerprint = recording_business_fingerprint(
            manifest.get("feature") or {},
            manifest.get("scenario") or {},
        )
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        FeatureDeliveryError,
        RecordingPackageError,
    ):
        return None
    return (
        run_path
        if recorded_fingerprint == expected_business_fingerprint
        else None
    )


def _read_run_manifest(run_path):
    try:
        value = json.loads(
            (Path(run_path) / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise FeatureDeliveryError(
            f"Recorder Run manifest无法读取: {run_path}"
        ) from error
    if not isinstance(value, dict):
        raise FeatureDeliveryError(
            f"Recorder Run manifest格式无效: {run_path}"
        )
    return value


def _run_manifest_step_progress(manifest):
    scenario_step_ids = {
        str(item.get("id") or "")
        for item in ((manifest.get("scenario") or {}).get("steps") or ())
        if isinstance(item, dict) and item.get("id")
    }
    steps = [
        item
        for item in (manifest.get("steps") or ())
        if isinstance(item, dict)
    ]
    declared = {
        str((item.get("plan") or {}).get("id") or "")
        for item in steps
        if (item.get("plan") or {}).get("id")
    }
    completed = {
        str((item.get("plan") or {}).get("id") or "")
        for item in steps
        if (item.get("plan") or {}).get("id")
        and item.get("status") == "completed"
    }
    completed_with_take = {
        str((item.get("plan") or {}).get("id") or "")
        for item in steps
        if (item.get("plan") or {}).get("id")
        and item.get("status") == "completed"
        and item.get("selected_take")
    }
    if any((
        not scenario_step_ids,
        not declared <= scenario_step_ids,
        not completed <= scenario_step_ids,
        completed != completed_with_take,
    )):
        raise ValueError("Scenario Step范围无效")
    return {
        "scenario_step_ids": scenario_step_ids,
        "declared_step_ids": declared,
        "completed_step_ids": completed,
        "completed_with_take_ids": completed_with_take,
        "recorded_step_count": len(completed_with_take),
        "missing_step_count": len(scenario_step_ids - completed_with_take),
        "total_step_count": len(scenario_step_ids),
        "scenario_recording_complete": (
            completed_with_take == scenario_step_ids
        ),
    }


def _validate_delivery_manifest(delivery):
    if not isinstance(delivery, dict):
        raise FeatureDeliveryError("Feature交付清单必须是object")
    feature = delivery.get("feature") or {}
    evidence = delivery.get("evidence") or {}
    runs = delivery.get("runs") or []
    policy = delivery.get("policy")
    if not isinstance(policy, dict):
        raise FeatureDeliveryError("Feature交付清单字段无效")
    scenario_count = _delivery_count(feature.get("scenario_count"))
    recorded_scenario_count = _delivery_count(
        feature.get("recorded_scenario_count")
    )
    complete_scenario_count = _delivery_count(
        feature.get("complete_scenario_count")
    )
    partial_scenario_count = _delivery_count(
        feature.get("partial_scenario_count")
    )
    evidence_run_count = _delivery_count(
        evidence.get("run_count")
    )
    all_scenarios_recorded = bool(
        scenario_count == len(runs)
        and complete_scenario_count == scenario_count
    )
    if any((
        delivery.get("feature_delivery_version") != FEATURE_DELIVERY_VERSION,
        not str(delivery.get("delivery_id") or "").startswith(
            "feature-delivery-"
        ),
        not isinstance(runs, list),
        not runs,
        not str(feature.get("id") or ""),
        not str(feature.get("source_relpath") or ""),
        not str(feature.get("source_hash") or ""),
        not str(feature.get("sha256") or ""),
        scenario_count < len(runs),
        recorded_scenario_count != len(runs),
        complete_scenario_count + partial_scenario_count
        != recorded_scenario_count,
        evidence.get("member") != EVIDENCE_MEMBER,
        evidence_run_count != len(runs),
        not str(evidence.get("sha256") or ""),
        policy.get("feature_included") is not True,
        policy.get("generation_state_included") is not False,
        policy.get("partial_recording_allowed") is not True,
        policy.get("export_scope") not in {"feature", "scenarios"},
        not isinstance(policy.get("all_scenarios_recorded"), bool),
        policy.get("all_scenarios_recorded") != all_scenarios_recorded,
    )):
        raise FeatureDeliveryError("Feature交付清单字段无效")
    for item in runs:
        if not isinstance(item, dict):
            raise FeatureDeliveryError("Feature交付清单字段无效")
        recorded_step_count = _delivery_count(
            item.get("recorded_step_count")
        )
        missing_step_count = _delivery_count(
            item.get("missing_step_count")
        )
        total_step_count = _delivery_count(item.get("total_step_count"))
        complete = item.get("scenario_recording_complete")
        if any((
            recorded_step_count <= 0,
            total_step_count <= 0,
            recorded_step_count + missing_step_count != total_step_count,
            not isinstance(complete, bool),
            complete != (missing_step_count == 0),
        )):
            raise FeatureDeliveryError("Feature交付清单字段无效")
    if any((
        complete_scenario_count != sum(
            item["scenario_recording_complete"] for item in runs
        ),
        partial_scenario_count != sum(
            not item["scenario_recording_complete"] for item in runs
        ),
    )):
        raise FeatureDeliveryError("Feature交付清单字段无效")
    session_ids = [str(item.get("session_id") or "") for item in runs]
    scenario_ids = [str(item.get("scenario_id") or "") for item in runs]
    if any((
        any(not value for value in session_ids),
        len(set(session_ids)) != len(session_ids),
        any(not value for value in scenario_ids),
        len(set(scenario_ids)) != len(scenario_ids),
    )):
        raise FeatureDeliveryError("Feature交付清单Run身份重复或缺失")


def _delivery_count(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise FeatureDeliveryError("Feature交付清单字段无效")
    return value


def _validate_delivery_run_progress(delivery_runs, evidence_path):
    try:
        evidence_runs = {
            item["session_id"]: item["manifest"]
            for item in inspect_recording_package_manifests(evidence_path)
        }
        if len(evidence_runs) != len(delivery_runs):
            raise ValueError("Run数量不一致")
        for delivery_run in delivery_runs:
            session_id = str(delivery_run.get("session_id") or "")
            manifest = evidence_runs.get(session_id)
            if manifest is None:
                raise ValueError("Run身份不一致")
            scenario = manifest.get("scenario") or {}
            if str(scenario.get("id") or "") != str(
                    delivery_run.get("scenario_id") or ""):
                raise ValueError("Scenario身份不一致")
            progress = _manifest_step_progress(manifest)
            if any((
                progress["recorded_step_count"]
                != delivery_run.get("recorded_step_count"),
                progress["missing_step_count"]
                != delivery_run.get("missing_step_count"),
                progress["total_step_count"]
                != delivery_run.get("total_step_count"),
                progress["scenario_recording_complete"]
                != delivery_run.get("scenario_recording_complete"),
            )):
                raise ValueError("Step进度不一致")
    except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            RecordingPackageError,
            zipfile.BadZipFile,
    ) as error:
        raise FeatureDeliveryError(
            "交付清单与内层录屏证据进度不一致"
        ) from error


def _manifest_step_progress(manifest):
    scenario_step_ids = {
        str(item.get("id") or "")
        for item in ((manifest.get("scenario") or {}).get("steps") or ())
        if item.get("id")
    }
    completed_step_ids = {
        str((item.get("plan") or {}).get("id") or "")
        for item in manifest.get("steps") or ()
        if item.get("status") == "completed" and item.get("selected_take")
    }
    if not scenario_step_ids or not completed_step_ids <= scenario_step_ids:
        raise ValueError("Scenario Step范围无效")
    return {
        "recorded_step_count": len(completed_step_ids),
        "missing_step_count": len(scenario_step_ids - completed_step_ids),
        "total_step_count": len(scenario_step_ids),
        "scenario_recording_complete": (
            completed_step_ids == scenario_step_ids
        ),
    }


def _safe_project_relative(value):
    text = str(value or "").replace("\\", "/")
    path = PurePosixPath(text)
    if (
            not text
            or path.is_absolute()
            or bool(PureWindowsPath(text).drive)
            or path.suffix.casefold() != ".feature"
            or any(
                part in {"", ".", ".."} or ":" in part
                for part in path.parts
            )
    ):
        raise FeatureDeliveryError(f"Feature项目相对路径无效: {value!r}")
    return Path(*path.parts)


def _project_features_with_id(project_root, feature_id):
    feature_id = str(feature_id or "").casefold()
    if not feature_id:
        return []
    project_root = Path(project_root).resolve()
    feature_root = project_root / "Bdd"
    if not feature_root.is_dir():
        return []
    matches = []
    for candidate in sorted(feature_root.rglob("*.feature")):
        if candidate.is_symlink():
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(project_root)
        except ValueError:
            continue
        try:
            source_text = resolved.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        if persistent_feature_id(source_text) == feature_id:
            matches.append(resolved)
    return matches


def _selected_feature_target(project_root, target_feature_path):
    target = Path(target_feature_path)
    if not target.is_absolute():
        target = Path(project_root).resolve() / target
    target = target.resolve()
    if target.suffix.casefold() != ".feature":
        raise FeatureDeliveryError("Feature保存位置必须是.feature文件")
    try:
        relative = target.relative_to(Path(project_root).resolve())
    except ValueError as error:
        raise FeatureDeliveryError("Feature保存位置必须位于当前项目内") from error
    return _project_feature_target(project_root, relative)


def _load_delivery_feature_plan(feature_path):
    project_root = Paths.BASE_DIR.resolve()
    candidate = Path(feature_path).absolute()
    for current in (candidate, *candidate.parents):
        if current.is_symlink():
            raise FeatureDeliveryError(
                f"Feature源路径不能包含符号链接: {current}"
            )
    resolved = candidate.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError as error:
        raise FeatureDeliveryError(
            f"Feature必须位于当前项目内: {candidate}"
        ) from error
    plan = load_feature_plan(resolved, ensure_identity=True)
    _safe_project_relative(plan.source_relpath)
    return plan


def _safe_run_relative(value):
    text = str(value or "").replace("\\", "/")
    path = PurePosixPath(text)
    if (
            not text
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise FeatureDeliveryError(f"Run相对路径无效: {value!r}")
    return Path(*path.parts)


def _project_feature_target(project_root, relative_path):
    project_root = Path(project_root).resolve()
    path = project_root.joinpath(*Path(relative_path).parts)
    current = project_root
    for part in Path(relative_path).parts:
        current = current / part
        if current.is_symlink():
            raise FeatureDeliveryError(
                f"Feature目标路径不能包含符号链接: {current}"
            )
    try:
        path.resolve().relative_to(project_root)
    except ValueError as error:
        raise FeatureDeliveryError("Feature目标路径越出项目目录") from error
    return path


def _install_feature(path, content, project_root):
    path = _project_feature_target(
        project_root,
        Path(path).relative_to(Path(project_root).resolve()),
    )
    if os.path.lexists(path):
        raise FeatureDeliveryError(f"目标Feature已存在: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path = _project_feature_target(
        project_root,
        path.relative_to(Path(project_root).resolve()),
    )
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".importing",
        dir=path.parent,
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _project_feature_target(
            project_root,
            path.relative_to(Path(project_root).resolve()),
        )
        _publish_without_overwrite(
            temporary,
            path,
            conflict_label="目标Feature导入期间发生冲突",
        )
    finally:
        temporary.unlink(missing_ok=True)


def _remove_empty_parents(path, root):
    path = Path(path)
    root = Path(root).resolve()
    while path != root:
        try:
            path.rmdir()
        except OSError:
            return
        path = path.parent


def _zip_info_is_symlink(info):
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _publish_without_overwrite(
        source,
        target,
        *,
        conflict_label="目标交付包已存在，禁止覆盖",
    ):
    source = Path(source)
    target = _safe_delivery_output_path(target)
    try:
        os.link(source, target)
    except FileExistsError as error:
        raise FeatureDeliveryError(f"{conflict_label}: {target}") from error
    except OSError as error:
        raise FeatureDeliveryError(
            f"无法原子发布文件 {target}: {type(error).__name__}: {error}"
        ) from error


def _safe_delivery_output_path(path):
    candidate = Path(path).absolute()
    for current in (candidate, *candidate.parents):
        if current.is_symlink():
            raise FeatureDeliveryError(
                f"Feature交付输出路径不能包含符号链接: {current}"
            )
    return candidate.resolve()


def _feature_conflict_summary(current_bytes, incoming_bytes):
    try:
        current = current_bytes.decode("utf-8-sig").splitlines()
        incoming = incoming_bytes.decode("utf-8-sig").splitlines()
    except UnicodeDecodeError:
        return (
            "目标Feature或传入Feature不是UTF-8文本；"
            "请在项目中人工比较后再重新导入。"
        )
    lines = list(difflib.unified_diff(
        current,
        incoming,
        fromfile="当前项目",
        tofile="传入交付包",
        lineterm="",
        n=2,
    ))
    limit = 24
    visible = lines[:limit]
    if len(lines) > limit:
        visible.append(f"... 另有 {len(lines) - limit} 行差异未显示")
    return "\n".join(visible) or "Feature字节不同。"


def _write_json_atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "FEATURE_DELIVERY_VERSION",
    "FeatureDeliveryError",
    "export_feature_delivery",
    "import_feature_delivery",
    "is_feature_delivery_package",
    "load_feature_delivery_index",
    "preview_feature_delivery",
    "record_feature_delivery",
]
