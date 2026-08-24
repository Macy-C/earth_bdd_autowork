from __future__ import annotations

import argparse
import configparser
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory


SMOKE_VERSION = "1.0"
REQUIRED_PATHS = (
    "autowork_core/__init__.py",
    "ai/manifest.json",
    "docs/1.快速开始.md",
    ".github/copilot-instructions.md",
    "resources/ffmpeg",
    "resources/models",
    "requirements.txt",
    "behave.ini",
    "README.md",
    ".gitattributes",
    ".gitignore",
    "Bdd/environment.py",
    "Bdd/runner.py",
    "config/config.yaml",
    "config/paths.py",
    "config/settings.py",
)
IMPORT_MODULES = (
    "autowork_core.actions",
    "autowork_core.common",
    "autowork_core.page",
    "autowork_core.runtime.behave_runner",
    "autowork_core.utils.debug_tools.recorder.generation_workflow",
    "Bdd.environment",
    "Bdd.runner",
)
EXPECTED_RUNNER = (
    "autowork_core.runtime.behave_runner:FeatureScopedRunner"
)
_IMPORT_CHECK_SCRIPT = r"""
import importlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
result_path = Path(sys.argv[2])
module_names = json.loads(sys.argv[3])
sys.path.insert(0, str(root))
errors = []

try:
    from config.paths import Paths
except Exception as error:
    errors.append(
        f"config.paths 导入失败: {type(error).__name__}: {error}"
    )
else:
    if Paths.BASE_DIR.resolve() != root:
        errors.append(
            "config.paths.Paths.BASE_DIR 与目标项目不一致: "
            f"{Paths.BASE_DIR.resolve()}"
        )

for module_name in module_names:
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        errors.append(
            f"模块导入失败: {module_name}: "
            f"{type(error).__name__}: {error}"
        )
        continue
    source = getattr(module, "__file__", None)
    try:
        Path(source).resolve().relative_to(root)
    except (TypeError, ValueError):
        errors.append(
            f"模块来源不属于目标项目: {module_name}: {source}"
        )

imported_validation_modules = sorted(
    name
    for name in sys.modules
    if name == "framework_validation"
    or name.startswith("framework_validation.")
)
result_path.write_text(
    json.dumps({
        "errors": errors,
        "imported_validation_modules": imported_validation_modules,
    }, ensure_ascii=False),
    encoding="utf-8",
)
"""


def inspect_framework_installation(project_root):
    root = Path(project_root).resolve()
    errors = []
    missing_paths = [
        relative
        for relative in REQUIRED_PATHS
        if not (root / relative).exists()
    ]
    errors.extend(
        f"缺少框架或宿主资产: {relative}"
        for relative in missing_paths
    )
    errors.extend(_behave_configuration_errors(root))

    import_errors, imported_validation_modules = _import_errors(root)
    errors.extend(import_errors)
    errors.extend(
        f"生产导入加载了验证资产: {name}"
        for name in imported_validation_modules
    )

    production_test_dirs = [
        path.relative_to(root).as_posix()
        for path in (root / "autowork_core").rglob("tests")
        if path.is_dir()
    ] if (root / "autowork_core").is_dir() else []
    errors.extend(
        f"生产包包含测试目录: {relative}"
        for relative in production_test_dirs
    )

    return {
        "framework_smoke_version": SMOKE_VERSION,
        "status": "passed" if not errors else "invalid",
        "project_root": str(root),
        "summary": {
            "required_paths": len(REQUIRED_PATHS),
            "missing_paths": len(missing_paths),
            "import_modules": len(IMPORT_MODULES),
            "production_test_directories": len(production_test_dirs),
        },
        "errors": errors,
    }


def _behave_configuration_errors(root):
    path = root / "behave.ini"
    if not path.is_file():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (OSError, configparser.Error) as error:
        return [f"behave.ini 无法读取: {type(error).__name__}: {error}"]
    if not parser.has_section("behave"):
        return ["behave.ini 缺少 [behave]"]
    errors = []
    if parser.get("behave", "paths", fallback="").strip() != "Bdd":
        errors.append("behave.ini paths 必须为 Bdd")
    if parser.get("behave", "runner", fallback="").strip() != EXPECTED_RUNNER:
        errors.append(f"behave.ini runner 必须为 {EXPECTED_RUNNER}")
    return errors


def _import_errors(root):
    root = Path(root).resolve()
    with TemporaryDirectory(prefix="bdd-autowork-smoke-") as temp_value:
        result_path = Path(temp_value) / "import-check.json"
        command = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _IMPORT_CHECK_SCRIPT,
            str(root),
            str(result_path),
            json.dumps(IMPORT_MODULES),
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return ([
                "目标项目导入检查启动失败: "
                f"{type(error).__name__}: {error}"
            ], [])
        if completed.returncode != 0 or not result_path.is_file():
            detail = (completed.stderr or completed.stdout or "").strip()
            return ([
                "目标项目导入检查失败"
                + (f": {detail}" if detail else "")
            ], [])
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return ([
                "目标项目导入检查结果无效: "
                f"{type(error).__name__}: {error}"
            ], [])
    errors = result.get("errors")
    imported = result.get("imported_validation_modules")
    if not isinstance(errors, list) or not isinstance(imported, list):
        return (["目标项目导入检查结果字段无效"], [])
    return ([str(item) for item in errors], [str(item) for item in imported])


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run lightweight post-sync framework checks",
    )
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args(argv)
    result = inspect_framework_installation(args.project_root)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
