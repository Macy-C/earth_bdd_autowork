"""Manage framework-generated local-debug Feature files."""

from __future__ import annotations

import re
import tempfile
from contextlib import contextmanager
from pathlib import Path


_TEMPORARY_FEATURE_KINDS = frozenset({"rerun", "single-step"})
_TEMPORARY_FEATURE_NAME = re.compile(
    r"^\..+\.(?:rerun|single-step)\.[A-Za-z0-9_-]+\.feature$"
)


def is_temporary_feature(path):
    return bool(_TEMPORARY_FEATURE_NAME.fullmatch(Path(path).name))


def remove_temporary_feature(path):
    path = Path(path)
    if not is_temporary_feature(path):
        raise ValueError(f"不是框架生成的临时 Feature: {path}")
    path.unlink(missing_ok=True)


def cleanup_temporary_features(feature_path):
    path = Path(feature_path)
    if path.is_dir():
        candidates = path.rglob("*.feature")
    elif path.is_file():
        candidates = path.parent.glob("*.feature")
    else:
        return ()

    removed = []
    for candidate in sorted(candidates):
        if not is_temporary_feature(candidate):
            continue
        remove_temporary_feature(candidate)
        removed.append(candidate)
    return tuple(removed)


@contextmanager
def generated_temporary_feature(source_path, kind, content):
    if kind not in _TEMPORARY_FEATURE_KINDS:
        raise ValueError(f"未知临时 Feature 类型: {kind}")

    source_path = Path(source_path)
    with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{source_path.stem}.{kind}.",
            suffix=".feature",
            dir=source_path.parent,
            delete=False,
    ) as stream:
        stream.write(content)
        generated_path = Path(stream.name)
    try:
        yield generated_path
    finally:
        remove_temporary_feature(generated_path)