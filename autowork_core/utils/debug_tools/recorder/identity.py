from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from config.paths import Paths


FEATURE_ID_MARKER = "recorder-feature-id"
FEATURE_ID_PATTERN = re.compile(r"^feature-[0-9a-f]{12,64}$")
FEATURE_ID_LINE = re.compile(
    rf"^\s*#\s*{FEATURE_ID_MARKER}\s*:\s*(\S+)\s*$",
    re.IGNORECASE,
)


def stable_digest(*parts, length=12):
    text = "\x1f".join(str(part or "").strip().casefold() for part in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def locator_candidate_id(locator, reason):
    payload = json.dumps(
        {
            "locator": dict(locator or {}),
            "reason": str(reason or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "locator-candidate-" + hashlib.sha256(payload).hexdigest()[:16]


def safe_segment(value, max_length=32, fallback="item"):
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", str(value or fallback))
    text = text.strip("_. ") or fallback
    if len(text) <= max_length:
        return text
    digest = stable_digest(text, length=8)
    return f"{text[:max_length - 9].rstrip('_')}_{digest}"


def key_segment(value, max_length=16, fallback="item"):
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", str(value or fallback))
    return (text.strip("_. ") or fallback)[:max_length].rstrip("_")


def display_segment(value, max_length=48, fallback="item"):
    text = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", str(value or fallback))
    text = re.sub(r"\s+", " ", text).strip(" ._") or fallback
    if len(text) <= max_length:
        return text
    digest = stable_digest(text, length=6)
    return f"{text[:max_length - 7].rstrip()}_{digest}"


def named_segment(value, max_length=32, fallback="item"):
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", str(value or fallback))
    text = text.strip("_. ") or fallback
    max_length = max(1, int(max_length))
    if len(text) <= max_length:
        return text
    prefix = text[:max_length].rstrip("_")
    if "_" in prefix:
        boundary = prefix.rsplit("_", 1)[0].rstrip("_")
        if len(boundary) >= max(4, max_length // 2):
            return boundary
    return prefix or fallback


def identity_suffix(value, length=6):
    text = str(value or "").strip().rsplit("-", 1)[-1]
    if re.fullmatch(r"[0-9a-fA-F]+", text or ""):
        return text[:length].lower()
    return stable_digest(value, length=length)


def feature_directory_name(feature_name, max_length=32, feature_id=None):
    suffix = f"_{identity_suffix(feature_id)}" if feature_id else ""
    name = named_segment(feature_name, max_length, "Unnamed_Feature")
    return f"Feature_{name}{suffix}"


def scenario_directory_name(
    scenario_name,
    example_id=None,
    max_length=28,
    scenario_id=None,
):
    suffix = (
        f"_Ex_{display_segment(example_id, 12, 'row')}"
        if example_id
        else ""
    )
    identity = f"_{identity_suffix(scenario_id)}" if scenario_id else ""
    name = named_segment(scenario_name, max_length, "Unnamed_Scenario")
    return f"Scenario_{name}{suffix}{identity}"


def run_directory_name(timestamp):
    return f"Run_{timestamp}"


def step_directory_name(step, max_text_length=28):
    raw_keyword = "Background " + step.keyword if step.is_background else step.keyword
    keyword = named_segment(raw_keyword, 16, "Step")
    text = named_segment(step.text, max_text_length, "Unnamed_Step")
    return (
        f"Step_{step.ordinal:03d}_{keyword}_{text}_"
        f"{identity_suffix(step.id)}"
    )


def compact_feature_directory_name(feature_name, feature_id):
    return feature_directory_name(
        feature_name,
        max_length=20,
        feature_id=feature_id,
    )


def compact_scenario_directory_name(scenario_name, scenario_id, example_id=None):
    return scenario_directory_name(
        scenario_name,
        example_id,
        max_length=12,
        scenario_id=scenario_id,
    )


def compact_run_directory_name(timestamp):
    date, clock, _milliseconds = timestamp.split("-", 2)
    return f"Run_{date[2:]}-{clock[:4]}"


def compact_step_directory_name(step):
    return (
        f"Step_{step.ordinal:03d}_"
        f"{named_segment(step.text, 8, 'Step')}_"
        f"{identity_suffix(step.id)}"
    )


def minimal_feature_directory_name(feature_id):
    return f"Feature_{identity_suffix(feature_id)}"


def minimal_scenario_directory_name(scenario_id, example_id=None):
    suffix = (
        f"_Ex_{named_segment(example_id, 8, 'row')}"
        if example_id
        else ""
    )
    return f"Scenario_{identity_suffix(scenario_id)}{suffix}"


def minimal_step_directory_name(step):
    return f"S{step.ordinal:03d}_{identity_suffix(step.id)}"


def source_relative_path(source_path):
    source_path = Path(source_path).resolve()
    try:
        return source_path.relative_to(Paths.BASE_DIR.resolve()).as_posix()
    except ValueError:
        return source_path.as_posix()


def feature_identity(source_path, feature_name, *, source_text=None):
    relative_path = source_relative_path(source_path)
    feature_id = persistent_feature_id(source_text)
    digest = (
        feature_id.removeprefix("feature-")
        if feature_id
        else stable_digest("feature", relative_path)
    )
    return {
        "id": feature_id or f"feature-{digest}",
        "key": f"f_{key_segment(feature_name, 16, 'feature')}_{digest[:8]}",
        "source_relpath": relative_path,
    }


def persistent_feature_id(source_text):
    if source_text is None:
        return None
    declared = []
    for line in str(source_text).splitlines():
        stripped = line.lstrip()
        if (
                not stripped.startswith("#")
                or FEATURE_ID_MARKER not in stripped.casefold()
        ):
            continue
        match = FEATURE_ID_LINE.fullmatch(line)
        if match is None:
            raise ValueError("Recorder Feature ID 标记格式无效")
        value = match.group(1).casefold()
        if FEATURE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("Recorder Feature ID 必须是 feature- 加 12-64 位十六进制")
        declared.append(value)
    if len(declared) > 1:
        raise ValueError("Recorder Feature ID 标记只能声明一次")
    return declared[0] if declared else None


def ensure_persistent_feature_id(source_path, feature_id):
    path = Path(source_path).resolve()
    feature_id = str(feature_id or "").casefold()
    if FEATURE_ID_PATTERN.fullmatch(feature_id) is None:
        raise ValueError("Recorder Feature ID 必须是 feature- 加 12-64 位十六进制")
    if path.is_symlink():
        raise ValueError("Recorder Feature ID 不能写入符号链接")
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    declared = persistent_feature_id(text)
    if declared is not None:
        if declared != feature_id:
            raise ValueError("Recorder Feature ID 与当前 Feature 身份不一致")
        return declared
    newline = "\r\n" if "\r\n" in text else "\n"
    updated = f"# {FEATURE_ID_MARKER}: {feature_id}{newline}{text}"
    payload = (b"\xef\xbb\xbf" if has_bom else b"") + updated.encode("utf-8")
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return feature_id


def scenario_identity(feature_id, name, kind, example_id, occurrence=1):
    digest = stable_digest(
        "scenario",
        feature_id,
        name,
        kind,
        example_id,
        occurrence,
    )
    suffix = f"_e{str(example_id).replace('.', '_')}" if example_id else ""
    return {
        "id": f"scenario-{digest}",
        "key": f"s_{key_segment(name, 16, 'scenario')}{suffix}_{digest[:8]}",
    }


def step_identity(scenario_id, keyword, text, is_background, occurrence=1):
    digest = stable_digest(
        "step",
        scenario_id,
        keyword,
        text,
        "background" if is_background else "scenario",
        occurrence,
    )
    return {
        "id": f"step-{digest}",
        "key": f"st_{key_segment(text, 16, 'step')}_{digest[:8]}",
    }