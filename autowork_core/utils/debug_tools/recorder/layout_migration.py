from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

from autowork_core.utils.debug_tools.recorder.identity import (
    compact_feature_directory_name,
    compact_scenario_directory_name,
    compact_step_directory_name,
    feature_directory_name,
    minimal_feature_directory_name,
    minimal_scenario_directory_name,
    minimal_step_directory_name,
    scenario_directory_name,
    step_directory_name,
)
from autowork_core.utils.debug_tools.recorder.session import (
    _session_paths_too_long,
)
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


def migrate_recording_layout(output_root, session_id=None):
    output_root = Path(output_root).resolve()
    catalog_path = output_root / "catalog.json"
    if not catalog_path.exists():
        return []
    catalog = _read_json(catalog_path)
    results = []
    for entry in list(catalog.get("sessions") or []):
        if session_id and entry.get("session_id") != session_id:
            continue
        session_dir = output_root / entry["path"]
        if not (session_dir / "manifest.json").exists():
            continue
        manifest = _read_json(session_dir / "manifest.json")
        if manifest.get("status") in {"open", "recording"}:
            continue
        result = _migrate_session(output_root, session_dir, manifest, catalog)
        if result is not None:
            results.append(result)
    if results:
        write_json_atomic(catalog_path, catalog)
    return results


def _migrate_session(output_root, session_dir, manifest, catalog):
    steps = [
        SimpleNamespace(**entry["plan"])
        for entry in manifest.get("steps", [])
    ]
    target_dir, mode = _target_session_dir(
        output_root,
        session_dir.name,
        manifest["feature"],
        manifest["scenario"],
        steps,
    )
    step_renames = _step_renames(manifest, steps, mode)
    old_relative = session_dir.relative_to(output_root).as_posix()
    new_relative = target_dir.relative_to(output_root).as_posix()
    if target_dir == session_dir and not step_renames:
        return None
    if target_dir.exists() and target_dir != session_dir:
        raise FileExistsError(f"目标录制目录已经存在: {target_dir}")

    replacements = [
        (str(session_dir), str(target_dir)),
        (session_dir.as_posix(), target_dir.as_posix()),
        (old_relative, new_relative),
        *step_renames,
    ]
    replacements.sort(key=lambda item: len(item[0]), reverse=True)

    json_documents = []
    for path in session_dir.rglob("*.json"):
        relative_path = path.relative_to(session_dir).as_posix()
        new_path = _replace_text(relative_path, step_renames)
        original = _read_json(path)
        transformed = _replace_value(original, replacements)
        json_documents.append((relative_path, new_path, original, transformed))

    original_catalog = json.loads(json.dumps(catalog))
    transformed_catalog = _replace_value(catalog, replacements)
    moved_outer = False
    completed_step_moves = []
    try:
        if target_dir != session_dir:
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            session_dir.rename(target_dir)
            moved_outer = True
        else:
            target_dir = session_dir

        for old_prefix, new_prefix in step_renames:
            old_step = target_dir / old_prefix
            new_step = target_dir / new_prefix
            if old_step == new_step or not old_step.exists():
                continue
            if new_step.exists():
                raise FileExistsError(f"目标 Step 目录已经存在: {new_step}")
            new_step.parent.mkdir(parents=True, exist_ok=True)
            old_step.rename(new_step)
            completed_step_moves.append((old_step, new_step))

        for _old_path, new_path, _original, transformed in json_documents:
            write_json_atomic(target_dir / new_path, transformed)
        catalog.clear()
        catalog.update(transformed_catalog)
    except Exception:
        for _old_path, new_path, original, _transformed in json_documents:
            current_path = target_dir / new_path
            if current_path.exists():
                write_json_atomic(current_path, original)
        for old_step, new_step in reversed(completed_step_moves):
            if new_step.exists() and not old_step.exists():
                new_step.rename(old_step)
        if moved_outer and target_dir.exists() and not session_dir.exists():
            session_dir.parent.mkdir(parents=True, exist_ok=True)
            target_dir.rename(session_dir)
        catalog.clear()
        catalog.update(original_catalog)
        raise

    if moved_outer:
        _remove_empty_parents(session_dir.parent, output_root)
    return {
        "session_id": manifest["session_id"],
        "old_path": old_relative,
        "new_path": new_relative,
        "path_mode": mode,
        "renamed_steps": len(step_renames),
    }


def _target_session_dir(output_root, run_name, feature, scenario, steps):
    candidates = (
        (
            "readable",
            feature_directory_name(
                feature["name"],
                feature_id=feature["id"],
            ),
            scenario_directory_name(
                scenario["name"],
                scenario.get("example_id"),
                scenario_id=scenario["id"],
            ),
        ),
        (
            "compact",
            compact_feature_directory_name(feature["name"], feature["id"]),
            compact_scenario_directory_name(
                scenario["name"],
                scenario["id"],
                scenario.get("example_id"),
            ),
        ),
        (
            "minimal",
            minimal_feature_directory_name(feature["id"]),
            minimal_scenario_directory_name(
                scenario["id"],
                scenario.get("example_id"),
            ),
        ),
    )
    for mode, feature_dir, scenario_dir in candidates:
        target = output_root / feature_dir / scenario_dir / run_name
        if not _session_paths_too_long(target, steps, mode=mode):
            return target, mode
    raise ValueError("输出根目录过长，无法迁移为最小录制目录结构")


def _step_renames(manifest, steps, mode):
    plan_by_id = {step.id: step for step in steps}
    renames = []
    for entry in manifest.get("steps", []):
        step = plan_by_id[entry["plan"]["id"]]
        target_name = (
            step_directory_name(step)
            if mode == "readable"
            else compact_step_directory_name(step)
            if mode == "compact"
            else minimal_step_directory_name(step)
        )
        old_directories = {
            Path(take["path"]).parts[1]
            for take in entry.get("takes", [])
            if len(Path(take["path"]).parts) >= 2
        }
        for old_name in old_directories:
            old_prefix = (Path("steps") / old_name).as_posix()
            new_prefix = (Path("steps") / target_name).as_posix()
            if old_prefix != new_prefix:
                renames.append((old_prefix, new_prefix))
    return sorted(set(renames))


def _replace_value(value, replacements):
    if isinstance(value, dict):
        return {
            key: _replace_value(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_value(item, replacements) for item in value]
    if isinstance(value, str):
        return _replace_text(value, replacements)
    return value


def _replace_text(value, replacements):
    result = value
    for old, new in replacements:
        result = result.replace(old, new)
    return result


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _remove_empty_parents(path, stop):
    path = Path(path)
    stop = Path(stop)
    while path != stop and stop in path.parents:
        try:
            path.rmdir()
        except OSError:
            break
        path = path.parent


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Migrate recorder directories to readable Feature/Scenario/Step names"
    )
    parser.add_argument("output_root")
    parser.add_argument("--session-id", default=None)
    args = parser.parse_args(argv)
    result = migrate_recording_layout(args.output_root, args.session_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
