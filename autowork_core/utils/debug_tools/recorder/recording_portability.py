from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PurePosixPath

from autowork_core.utils.debug_tools.recorder.catalog import (
    load_recording_catalog,
)
from autowork_core.utils.debug_tools.recorder.generation_request import (
    build_generation_request,
)
from autowork_core.utils.debug_tools.recorder.models import SCHEMA_VERSION
from autowork_core.utils.debug_tools.recorder.run_lock import RunWriteLock
from autowork_core.utils.debug_tools.recorder.session_projection import (
    rebuild_session_projections,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


PACKAGE_VERSION = "1.0"
PACKAGE_MANIFEST = "recording-package.json"
RUN_PREFIX = PurePosixPath("runs")
MAX_PACKAGE_MEMBERS = 200_000
MAX_PACKAGE_BYTES = 50 * 1024 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024 * 1024


class RecordingPackageError(RuntimeError):
    pass


def export_recording_package(source, output):
    source = Path(source).resolve()
    output = Path(output).resolve()
    recording_root, runs = _discover_runs(source)
    return _export_recording_runs(
        recording_root,
        runs,
        output,
        source_boundary=source,
    )


def export_recording_runs(recording_root, runs, output):
    recording_root = Path(recording_root).resolve()
    runs = [Path(run).resolve() for run in runs]
    return _export_recording_runs(
        recording_root,
        runs,
        Path(output).resolve(),
        source_boundary=recording_root,
    )


def _export_recording_runs(
        recording_root,
        runs,
        output,
        *,
        source_boundary,
    ):
    if not runs:
        raise RecordingPackageError("没有可导出的 Recorder Run")
    for run in runs:
        try:
            run.relative_to(recording_root)
        except ValueError as error:
            raise RecordingPackageError(
                f"Recorder Run 越出录屏根目录: {run}"
            ) from error
    try:
        output.relative_to(source_boundary)
    except ValueError:
        pass
    else:
        raise RecordingPackageError("便携包不能写入待导出的录屏目录内部")

    with _export_snapshot(runs):
        run_entries = []
        archive_files = []
        session_ids = set()
        for run in runs:
            manifest = _read_json(run / "manifest.json")
            session_id = str(manifest.get("session_id") or "")
            if not session_id or session_id in session_ids:
                raise RecordingPackageError(
                    f"Run session_id 缺失或重复: {run}"
                )
            session_ids.add(session_id)
            validate_exportable_recording_run(run, manifest)
            relative_path = run.relative_to(recording_root).as_posix()
            run_files = []
            for path in sorted(run.rglob("*")):
                if not path.is_file() or not _portable_run_file(run, path):
                    continue
                relative = path.relative_to(run).as_posix()
                archive_path = (RUN_PREFIX / relative_path / relative).as_posix()
                item = {
                    "path": archive_path,
                    "sha256": _sha256(path),
                    "size": path.stat().st_size,
                }
                run_files.append(item)
                archive_files.append((path, item))
            if not any(
                    item["path"].endswith("/manifest.json")
                    for item in run_files
            ):
                raise RecordingPackageError(f"Run 缺少 manifest.json: {run}")
            run_entries.append({
                "session_id": session_id,
                "relative_path": relative_path,
                "manifest_sha256": _sha256(run / "manifest.json"),
                "file_count": len(run_files),
                "total_bytes": sum(item["size"] for item in run_files),
            })

        package = {
            "recording_package_version": PACKAGE_VERSION,
            "package_id": "recording-package-" + uuid.uuid4().hex,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "source_schema_version": SCHEMA_VERSION,
            "run_count": len(run_entries),
            "runs": run_entries,
            "files": [item for _path, item in archive_files],
            "policy": {
                "raw_evidence_modified": False,
                "generation_state_included": False,
            },
        }
        output.parent.mkdir(parents=True, exist_ok=True)
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
                    PACKAGE_MANIFEST,
                    json.dumps(package, ensure_ascii=False, indent=2),
                )
                for source_path, item in archive_files:
                    archive.write(source_path, item["path"])
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        result = {
            "recording_package_version": PACKAGE_VERSION,
            "package_id": package["package_id"],
            "package_path": str(output),
            "package_sha256": _sha256(output),
            "run_count": len(run_entries),
            "runs": run_entries,
        }
    return result


def import_recording_package(
        package_path,
        recording_root,
        *,
        source_relpath_override=None,
    ):
    package_path = Path(package_path).resolve()
    recording_root = Path(recording_root).resolve()
    if not package_path.is_file():
        raise FileNotFoundError(f"Recorder 便携包不存在: {package_path}")
    recording_root.mkdir(parents=True, exist_ok=True)
    catalog_path = recording_root / "catalog.json"
    catalog_existed = catalog_path.exists()
    catalog_bytes = catalog_path.read_bytes() if catalog_existed else None
    if not catalog_existed:
        write_json_atomic(catalog_path, {
            "schema_version": SCHEMA_VERSION,
            "updated_at": None,
            "sessions": [],
        })

    committed = []
    with tempfile.TemporaryDirectory(
            prefix=".recording-import-",
            dir=recording_root,
    ) as staging_value:
        staging = Path(staging_value)
        try:
            package = _extract_verified_package(package_path, staging)
            _validate_staged_runs(package, staging)
            existing_ids = {
                str(entry.get("session_id") or "")
                for entry in load_recording_catalog(recording_root).get(
                    "sessions"
                ) or ()
            }
            for path in recording_root.rglob("manifest.json"):
                try:
                    path.relative_to(staging)
                except ValueError:
                    pass
                else:
                    continue
                try:
                    existing_ids.add(str(_read_json(path).get("session_id") or ""))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
            existing_ids.discard("")
            destinations = []
            destination_keys = set()
            for run in package["runs"]:
                relative = _safe_relative_path(run["relative_path"])
                source_run = staging / RUN_PREFIX.as_posix() / relative
                destination = _contained(recording_root, recording_root / relative)
                session_id = str(run.get("session_id") or "")
                destination_key = str(destination).casefold()
                if destination_key in destination_keys:
                    raise RecordingPackageError(
                        f"便携包 Run 目标路径冲突: {destination}"
                    )
                destination_keys.add(destination_key)
                if destination.exists():
                    raise RecordingPackageError(
                        f"目标 Run 已存在，禁止覆盖: {destination}"
                    )
                if session_id in existing_ids:
                    raise RecordingPackageError(
                        f"目标 catalog 已存在相同 session_id: {session_id}"
                    )
                destinations.append((run, source_run, destination))

            for run, source_run, destination in destinations:
                destination.parent.mkdir(parents=True, exist_ok=True)
                _promote_imported_run(
                    source_run,
                    destination,
                    package,
                    run,
                )
                committed.append(destination)

            imported_runs = []
            source_override = (
                _bdd_relative_path(source_relpath_override)
                if source_relpath_override
                else None
            )
            if source_relpath_override and source_override is None:
                raise RecordingPackageError(
                    "Feature source_relpath覆盖值无效"
                )
            for run, _source_run, destination in destinations:
                manifest_path = destination / "manifest.json"
                manifest = _read_json(manifest_path)
                capture_config = dict(manifest.get("capture_config") or {})
                capture_config["output_root"] = str(recording_root)
                manifest["capture_config"] = capture_config
                feature = dict(manifest.get("feature") or {})
                feature["source_relpath"] = (
                    source_override
                    or _target_source_reference(
                        destination,
                        recording_root,
                        feature.get("source_relpath"),
                    )
                )
                manifest["feature"] = feature
                write_json_atomic(manifest_path, manifest)
                _rebase_import_metadata(
                    destination,
                    recording_root,
                    manifest,
                )
                readiness = rebuild_session_projections(
                    destination,
                    manifest=manifest,
                    output_root=recording_root,
                )
                if not readiness.get("bundle_valid"):
                    raise RecordingPackageError(
                        f"导入后证据校验失败: {destination}: "
                        f"{readiness.get('errors') or []}"
                    )
                request_path = None
                request_error = None
                completed_step_ids = [
                    str((entry.get("plan") or {}).get("id") or "")
                    for entry in manifest.get("steps") or []
                    if entry.get("status") == "completed"
                    and entry.get("selected_take")
                ]
                if completed_step_ids:
                    try:
                        request = build_generation_request(
                            destination,
                            steps=completed_step_ids,
                            write=True,
                            repair=True,
                            initialize_workflow=False,
                        )
                        request_path = str(
                            destination / request["request_path"]
                        )
                    except Exception as error:
                        raise RecordingPackageError(
                            f"导入后 Request 重建失败: {destination}: "
                            f"{type(error).__name__}: {error}"
                        ) from error
                elif manifest.get("status") == "finalized":
                    raise RecordingPackageError(
                        f"已完成 Run 没有可生成的 Step: {destination}"
                    )
                else:
                    request_error = "Run 尚无已完成 Step，可在目标机器继续录制"
                imported_runs.append({
                    "session_id": run["session_id"],
                    "session_dir": str(destination),
                    "request_path": request_path,
                    "request_error": request_error,
                    "bundle_valid": True,
                    "status": (
                        "ready_for_generation"
                        if request_path
                        else "needs_recording"
                    ),
                })
            return {
                "recording_package_version": PACKAGE_VERSION,
                "package_id": package["package_id"],
                "package_path": str(package_path),
                "package_sha256": _sha256(package_path),
                "recording_root": str(recording_root),
                "run_count": len(imported_runs),
                "runs": imported_runs,
            }
        except Exception:
            for destination in reversed(committed):
                shutil.rmtree(destination, ignore_errors=True)
            if catalog_existed:
                catalog_path.write_bytes(catalog_bytes)
            else:
                catalog_path.unlink(missing_ok=True)
            raise


def _discover_runs(source):
    if (source / "manifest.json").is_file():
        recording_root = _find_recording_root(source)
        return recording_root, [source]
    catalog_path = source / "catalog.json"
    if not catalog_path.is_file():
        raise RecordingPackageError(
            "导出源必须是一个 Run 目录或 recording_sessions 根目录"
        )
    runs = [
        path.parent
        for path in sorted(source.rglob("manifest.json"))
        if path.parent != source
    ]
    return source, runs


def validate_exportable_recording_run(run, manifest):
    status = str(manifest.get("status") or "")
    if status not in {"closed", "finalized"}:
        raise RecordingPackageError(
            f"Run 尚未关闭，不能导出: {run}: status={status or 'unknown'}"
        )
    running = _running_transactions(run)
    if running:
        raise RecordingPackageError(
            f"Run 存在运行中的生成事务，不能导出: {run}: {running}"
        )


@contextmanager
def _export_snapshot(runs):
    locks = []
    try:
        for run in sorted(
                (Path(path).resolve() for path in runs),
                key=lambda path: str(path).casefold(),
        ):
            try:
                locks.append(RunWriteLock(run).acquire())
            except RuntimeError as error:
                raise RecordingPackageError(
                    f"Run 正被写入，不能导出: {run}: {error}"
                ) from error
        yield
    finally:
        for lock in reversed(locks):
            lock.release()


def _portable_run_file(run, path):
    relative = path.relative_to(run)
    if path.name == ".recorder.lock":
        return False
    if relative.parts and relative.parts[0] == "ai":
        return False
    return True


def _running_transactions(run):
    result = []
    for path in (run / "ai" / "generation-transactions").glob(
            "transaction-*/report.json"
    ):
        try:
            value = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if value.get("status") == "running":
            result.append(str(value.get("transaction_id") or path.parent.name))
    for path in (run / "ai" / "workflow").glob("*.json"):
        try:
            value = _read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if value.get("status") == "running":
            result.append(str(value.get("request_id") or path.stem))
    return sorted(set(result))


def _rebase_import_metadata(run, recording_root, manifest):
    feature_plan_path = run / "feature-plan.json"
    if feature_plan_path.is_file():
        feature_plan = _read_json(feature_plan_path)
        feature = dict(feature_plan.get("feature") or {})
        source_relpath = str((manifest.get("feature") or {}).get(
            "source_relpath"
        ) or "")
        project_root = _project_root_for_recording_root(recording_root)
        source_candidate = Path(source_relpath)
        project_feature = (
            source_candidate
            if source_candidate.is_absolute()
            else project_root / source_candidate
            if project_root is not None and source_relpath
            else None
        )
        feature["source_path"] = str(
            project_feature.resolve()
            if project_feature is not None and project_feature.is_file()
            else (run / "source.feature").resolve()
        )
        feature_plan["feature"] = feature
        write_json_atomic(feature_plan_path, feature_plan)

    scenario_path = run / "scenario.json"
    if scenario_path.is_file():
        scenario = _read_json(scenario_path)
        capture_config = dict(scenario.get("capture_config") or {})
        capture_config["output_root"] = str(recording_root)
        scenario["capture_config"] = capture_config
        write_json_atomic(scenario_path, scenario)


def _target_source_reference(run, recording_root, source_value):
    source_relpath = _bdd_relative_path(source_value)
    project_root = _project_root_for_recording_root(recording_root)
    if project_root is not None and source_relpath:
        target = project_root / source_relpath
        if target.is_file():
            return source_relpath
    return str((run / "source.feature").resolve())


def _project_root_for_recording_root(recording_root):
    recording_root = Path(recording_root).resolve()
    if (
        recording_root.name == "recording_sessions"
        and recording_root.parent.name == "artifacts"
    ):
        return recording_root.parent.parent
    return None


def _bdd_relative_path(value):
    text = str(value or "").replace("\\", "/")
    parts = PurePosixPath(text).parts
    index = next(
        (offset for offset, part in enumerate(parts) if part.casefold() == "bdd"),
        None,
    )
    if index is not None:
        return PurePosixPath(*parts[index:]).as_posix()
    path = PurePosixPath(text)
    if not path.is_absolute() and ".." not in path.parts:
        return path.as_posix()
    return None


def _extract_verified_package(package_path, staging):
    with zipfile.ZipFile(package_path, "r") as archive:
        infos = archive.infolist()
        if len(infos) > MAX_PACKAGE_MEMBERS:
            raise RecordingPackageError("Recorder 便携包文件数量超过安全上限")
        if sum(info.file_size for info in infos) > MAX_PACKAGE_BYTES:
            raise RecordingPackageError("Recorder 便携包解压大小超过安全上限")
        by_name = {}
        canonical_names = set()
        for info in infos:
            name = _safe_archive_name(info.filename)
            canonical = name.casefold()
            if name in by_name or canonical in canonical_names:
                raise RecordingPackageError(f"便携包包含重复路径: {name}")
            canonical_names.add(canonical)
            if _zip_info_is_symlink(info):
                raise RecordingPackageError(f"便携包不能包含符号链接: {name}")
            if info.file_size > MAX_FILE_BYTES:
                raise RecordingPackageError(f"便携包单文件超过安全上限: {name}")
            by_name[name] = info
        manifest_info = by_name.get(PACKAGE_MANIFEST)
        if manifest_info is None:
            raise RecordingPackageError("Recorder 便携包缺少 package manifest")
        package = json.loads(archive.read(manifest_info).decode("utf-8"))
        _validate_package_manifest(package)
        declared = {
            str(item["path"]): item
            for item in package.get("files") or []
        }
        if len(declared) != len(package.get("files") or []):
            raise RecordingPackageError("Recorder 便携包文件清单包含重复路径")
        actual = {
            name
            for name, info in by_name.items()
            if name != PACKAGE_MANIFEST and not info.is_dir()
        }
        if actual != set(declared):
            raise RecordingPackageError(
                "Recorder 便携包文件清单与 ZIP 内容不一致"
            )
        for name in sorted(actual):
            item = declared[name]
            info = by_name[name]
            target = _contained(staging, staging / _safe_relative_path(name))
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            size = 0
            with archive.open(info, "r") as source, target.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            declared_size = item.get("size")
            if declared_size is None or size != int(declared_size):
                raise RecordingPackageError(f"便携包文件大小不匹配: {name}")
            if digest.hexdigest() != item.get("sha256"):
                raise RecordingPackageError(f"便携包文件 SHA-256 不匹配: {name}")
        return package


def _validate_package_manifest(package):
    if package.get("recording_package_version") != PACKAGE_VERSION:
        raise RecordingPackageError("不支持的 Recorder 便携包版本")
    runs = package.get("runs") or []
    if int(package.get("run_count") or 0) != len(runs) or not runs:
        raise RecordingPackageError("Recorder 便携包 Run 清单无效")
    session_ids = set()
    paths = set()
    for run in runs:
        session_id = str(run.get("session_id") or "")
        relative = _safe_relative_path(run.get("relative_path"))
        relative_key = relative.as_posix().casefold()
        if (
            not session_id
            or session_id in session_ids
            or relative_key in paths
            or not str(run.get("manifest_sha256") or "")
            or int(run.get("file_count") or 0) <= 0
            or int(run.get("total_bytes") or -1) < 0
        ):
            raise RecordingPackageError("Recorder 便携包 Run 身份重复或缺失")
        session_ids.add(session_id)
        paths.add(relative_key)


def _validate_staged_runs(package, staging):
    files = package.get("files") or []
    claimed_paths = set()
    for run in package.get("runs") or []:
        relative = _safe_relative_path(run["relative_path"])
        prefix = (RUN_PREFIX / PurePosixPath(relative.as_posix())).as_posix() + "/"
        run_files = [item for item in files if str(item.get("path") or "").startswith(prefix)]
        claimed_paths.update(str(item["path"]) for item in run_files)
        if len(run_files) != int(run["file_count"]):
            raise RecordingPackageError(
                f"Recorder Run 文件数量与清单不一致: {run['relative_path']}"
            )
        if sum(int(item["size"]) for item in run_files) != int(run["total_bytes"]):
            raise RecordingPackageError(
                f"Recorder Run 文件大小与清单不一致: {run['relative_path']}"
            )
        run_dir = staging / RUN_PREFIX.as_posix() / relative
        manifest_path = run_dir / "manifest.json"
        if not manifest_path.is_file():
            raise RecordingPackageError(
                f"Recorder Run 缺少 manifest.json: {run['relative_path']}"
            )
        manifest = _read_json(manifest_path)
        if str(manifest.get("session_id") or "") != str(run["session_id"]):
            raise RecordingPackageError(
                f"Recorder Run session_id 与包清单不一致: {run['relative_path']}"
            )
        if _sha256(manifest_path) != run["manifest_sha256"]:
            raise RecordingPackageError(
                f"Recorder Run manifest SHA-256 与包清单不一致: "
                f"{run['relative_path']}"
            )
        unexpected = [
            item["path"] for item in run_files
            if not str(item["path"]).startswith(prefix)
        ]
        if unexpected:
            raise RecordingPackageError(
                f"Recorder Run 文件路径与清单不一致: {unexpected[:3]}"
            )
    unclaimed = sorted(
        str(item.get("path") or "")
        for item in files
        if str(item.get("path") or "") not in claimed_paths
    )
    if unclaimed:
        raise RecordingPackageError(
            f"Recorder 便携包包含未归属 Run 的文件: {unclaimed[:3]}"
        )


def _promote_imported_run(source_run, destination, package, run):
    try:
        source_run.replace(destination)
        return
    except PermissionError:
        pass
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.importing"
    )
    try:
        shutil.copytree(source_run, temporary)
        _verify_run_files(temporary, package, run)
        temporary.replace(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        shutil.rmtree(destination, ignore_errors=True)
        raise
    shutil.rmtree(source_run, ignore_errors=True)


def _verify_run_files(run_dir, package, run):
    relative = _safe_relative_path(run["relative_path"])
    prefix = (RUN_PREFIX / PurePosixPath(relative.as_posix())).as_posix() + "/"
    expected = {
        str(item["path"])[len(prefix):]: item
        for item in package.get("files") or []
        if str(item.get("path") or "").startswith(prefix)
    }
    actual = {
        path.relative_to(run_dir).as_posix(): path
        for path in run_dir.rglob("*")
        if path.is_file()
    }
    if set(actual) != set(expected):
        raise RecordingPackageError(
            f"Recorder Run 复制后文件清单不一致: {run['relative_path']}"
        )
    for relative_path, path in actual.items():
        item = expected[relative_path]
        if path.stat().st_size != int(item["size"]):
            raise RecordingPackageError(
                f"Recorder Run 复制后文件大小不一致: {relative_path}"
            )
        if _sha256(path) != item["sha256"]:
            raise RecordingPackageError(
                f"Recorder Run 复制后 SHA-256 不一致: {relative_path}"
            )


def _find_recording_root(run):
    for candidate in run.parents:
        if (candidate / "catalog.json").is_file():
            return candidate.resolve()
    raise RecordingPackageError(
        f"无法从 Run 定位 recording_sessions 根目录: {run}"
    )


def _safe_archive_name(value):
    name = str(value or "").replace("\\", "/")
    if name.startswith("/") or "\x00" in name:
        raise RecordingPackageError(f"便携包路径无效: {value!r}")
    path = PurePosixPath(name)
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise RecordingPackageError(f"便携包路径越界: {value!r}")
    return path.as_posix()


def _safe_relative_path(value):
    return Path(*PurePosixPath(_safe_archive_name(value)).parts)


def _contained(root, path):
    root = Path(root).resolve()
    path = Path(path).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise RecordingPackageError(f"便携包路径越界: {path}") from error
    return path


def _zip_info_is_symlink(info):
    return ((info.external_attr >> 16) & 0o170000) == 0o120000


def _read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 必须是 object: {path}")
    return value


def _sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export or import portable Recorder evidence packages"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("source", help="recording_sessions root or one Run")
    export.add_argument("output", help="output .zip path")
    import_command = commands.add_parser("import")
    import_command.add_argument("package", help="portable Recorder .zip")
    import_command.add_argument(
        "recording_root",
        nargs="?",
        default="artifacts/recording_sessions",
    )
    args = parser.parse_args(argv)
    if args.command == "export":
        result = export_recording_package(args.source, args.output)
    else:
        result = import_recording_package(args.package, args.recording_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "PACKAGE_VERSION",
    "RecordingPackageError",
    "export_recording_package",
    "export_recording_runs",
    "import_recording_package",
]