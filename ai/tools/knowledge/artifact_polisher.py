from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

from autowork_core.knowledge.weknora_client import PublishError
from autowork_core.knowledge.case_verifier import verify_case_markdown


HASH_LINE = re.compile(r"(?m)^- 内容指纹（Content Hash）：`[a-fA-F0-9]{64}`\r?\n")
HEADING = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
CHAPTER = re.compile(r"^##\s+([1-7])\.")
IMMUTABLE_TOKEN = re.compile(r"`[^`\r\n]+`|\b[FTQ]\d+\b|\b\d+\.\d+\b|resource://\S+", re.IGNORECASE)


def _sha256(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required_tokens(text):
    return sorted(IMMUTABLE_TOKEN.findall(text))


def _is_plain_prose(line):
    text = line.strip()
    return (
        bool(text)
        and not text.startswith(("#", "|", "![", "<", ">", "- ", "* ", "**", "```", "Action ", "Image "))
        and not re.match(r"\d+\.\s", text)
    )


def _prose_blocks(text):
    lines = text.splitlines(keepends=True)
    positions = []
    offset = 0
    chapter = None
    heading = ""
    in_fence = False
    paragraph_start = None
    paragraph_end = None

    def append_paragraph():
        nonlocal paragraph_start, paragraph_end
        if paragraph_start is None:
            return
        source = text[paragraph_start:paragraph_end]
        if source.strip():
            positions.append((chapter, heading, paragraph_start, paragraph_end, source))
        paragraph_start = paragraph_end = None

    for line in lines:
        clean = line.rstrip("\r\n")
        match = HEADING.match(clean)
        if clean.lstrip().startswith("```"):
            append_paragraph()
            in_fence = not in_fence
        if not in_fence and match:
            append_paragraph()
            heading = match.group(2)
            chapter_match = CHAPTER.match(clean)
            if chapter_match:
                chapter = int(chapter_match.group(1))
        elif not in_fence and chapter in {3, 4} and _is_plain_prose(clean):
            if paragraph_start is None:
                paragraph_start = offset
            paragraph_end = offset + len(line)
        else:
            append_paragraph()
        offset += len(line)
    append_paragraph()

    title = next((match for match in HEADING.finditer(text) if len(match.group(1)) == 1), None)
    blocks = []
    if title:
        blocks.append(("title", "标题", title.start(2), title.end(2), title.group(2)))
    for index, (chapter, heading, start, end, source) in enumerate(positions, 1):
        blocks.append((f"prose-{index:03}", heading or f"第 {chapter} 章", start, end, source))
    return blocks


def inspect_markdown_for_polish(markdown_path, *, output_spec_path=None, persist_spec=True):
    """Extract only editable prose slots from one complete Markdown artifact."""
    source_path = Path(markdown_path).resolve()
    source_bytes = source_path.read_bytes()
    source_text = source_bytes.decode("utf-8-sig")
    verification = verify_case_markdown(source_path)
    if verification["status"] != "passed":
        raise PublishError(
            "invalid_document", "Selected Markdown cannot be polished safely until structural verification passes",
            stage="inspect-file", details={"verification_errors": verification["errors"]},
        )
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    blocks = [
        {
            "id": block_id,
            "heading": heading,
            "source_text": source,
            "source_sha256": _sha256(source),
            "required_tokens": _required_tokens(source),
            "replacement": source,
        }
        for block_id, heading, _start, _end, source in _prose_blocks(source_text)
    ]
    if not blocks:
        raise PublishError("no_polishable_content", "Selected Markdown has no safe title or prose slots for AI polishing", stage="inspect-file")
    default_spec = source_path.with_name(f"{source_path.stem}.{source_sha256[:12]}.polish.json")
    spec_path = Path(output_spec_path).resolve() if output_spec_path else default_spec
    specification = {
        "schema_version": "1.0",
        "kind": "explicit_markdown_polish",
        "source_path": str(source_path),
        "source_sha256": source_sha256,
        "polished_by": "pending_ai",
        "blocks": blocks,
    }
    if spec_path.exists():
        existing = json.loads(spec_path.read_text(encoding="utf-8-sig"))
        if any(existing.get(key) != specification[key] for key in ("schema_version", "kind", "source_path", "source_sha256")):
            raise PublishError("polish_spec_conflict", "Existing explicit-file polish specification belongs to different source bytes", stage="inspect-file")
        return {"status": "inspected", "source_path": str(source_path), "polish_spec_path": str(spec_path), "specification": existing}
    if not persist_spec:
        return {"status": "inspected", "source_path": str(source_path), "polish_spec_path": None, "specification": specification}
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    with spec_path.open("x", encoding="utf-8") as destination:
        json.dump(specification, destination, ensure_ascii=False, indent=2)
        destination.write("\n")
    return {"status": "inspected", "source_path": str(source_path), "polish_spec_path": str(spec_path), "specification": specification}


def apply_markdown_polish(markdown_path, polish_spec_path, *, output_path=None, replacements=None, polished_by=None, expected_source_sha256=None):
    """Create a verified polished copy without modifying the selected Markdown source."""
    source_path = Path(markdown_path).resolve()
    source_bytes = source_path.read_bytes()
    source_text = source_bytes.decode("utf-8-sig")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    spec_path = Path(polish_spec_path).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    if spec.get("kind") != "explicit_markdown_polish" or spec.get("source_path") != str(source_path) or spec.get("source_sha256") != source_sha256:
        raise PublishError("source_changed", "Polish specification does not match the selected source bytes", stage="apply-polish")
    if replacements is not None:
        if expected_source_sha256 != source_sha256:
            raise PublishError("source_changed", "Source does not match the inspected hash", stage="apply-polish")
        if not isinstance(replacements, dict) or not replacements:
            raise PublishError("polish_spec_invalid", "Provide explicit block replacements", stage="apply-polish")
        blocks = spec.get("blocks") or []
        if any(not isinstance(block, dict) for block in blocks) or set(replacements) - {block.get("id") for block in blocks}:
            raise PublishError("polish_spec_invalid", "Replacement contains an unknown block ID", stage="apply-polish")
        spec = {
            **spec, "polished_by": polished_by,
            "blocks": [dict(block) for block in blocks],
        }
        for block in spec["blocks"]:
            replacement = replacements.get(block["id"], block["source_text"])
            ending = re.search(r"(\r\n|\n|\r)$", block["source_text"])
            if isinstance(replacement, str) and ending and not replacement.endswith(("\r", "\n")):
                replacement += ending.group(1)
            block["replacement"] = replacement
    if spec.get("polished_by") in {None, "", "pending_ai"}:
        raise PublishError("polish_not_completed", "AI must set polished_by after reviewing the prose specification", stage="apply-polish")

    actual = {block_id: (start, end, source) for block_id, _heading, start, end, source in _prose_blocks(source_text)}
    replacements = []
    seen = set()
    for block in spec.get("blocks") or []:
        block_id = block.get("id")
        if not isinstance(block_id, str) or block_id in seen or block_id not in actual:
            raise PublishError("polish_spec_invalid", "Polish specification has an unknown or duplicate prose block", stage="apply-polish")
        seen.add(block_id)
        start, end, original = actual[block_id]
        if block.get("source_text") != original or block.get("source_sha256") != _sha256(original):
            raise PublishError("source_changed", "A prose block no longer matches the selected source", stage="apply-polish")
        replacement = block.get("replacement")
        if not isinstance(replacement, str) or not replacement.strip():
            raise PublishError("polish_spec_invalid", "Every polished prose block must contain nonempty replacement text", stage="apply-polish")
        if _required_tokens(replacement) != block.get("required_tokens"):
            raise PublishError("polish_token_changed", "Polishing may not add, remove, or alter action, image, control, URL, or code tokens", stage="apply-polish")
        replacements.append((start, end, replacement))

    polished = source_text
    for start, end, replacement in sorted(replacements, reverse=True):
        polished = polished[:start] + replacement + polished[end:]
    without_hash = HASH_LINE.sub("", polished)
    fingerprint = _sha256(without_hash)
    if HASH_LINE.search(polished):
        polished = HASH_LINE.sub(f"- 内容指纹（Content Hash）：`{fingerprint}`\n", polished, count=1)
    output_bytes = polished.encode("utf-8")
    output_sha256 = hashlib.sha256(output_bytes).hexdigest()
    destination_path = Path(output_path).resolve() if output_path else source_path.with_name(
        f"{source_path.stem}.polished.{output_sha256[:12]}{source_path.suffix}"
    )
    if destination_path == source_path:
        raise PublishError("output_conflict", "Polished output must be a new file; the selected source is immutable", stage="apply-polish")
    if destination_path.exists() and destination_path.read_bytes() != output_bytes:
        raise PublishError("output_conflict", "A different file already occupies the polished output path", stage="apply-polish")
    created_output = False
    if not destination_path.exists():
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        with destination_path.open("xb") as destination:
            destination.write(output_bytes)
        created_output = True
    verification = verify_case_markdown(destination_path)
    if verification["status"] != "passed":
        if created_output:
            destination_path.unlink(missing_ok=True)
        raise PublishError(
            "polished_document_invalid", "Polished copy failed structural verification; the source was preserved",
            stage="apply-polish", details={"verification_errors": verification["errors"]},
        )
    return {
        "status": "polished",
        "source_path": str(source_path),
        "source_sha256": source_sha256,
        "output_path": str(destination_path),
        "output_sha256": output_sha256,
        "polished_by": spec["polished_by"],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Protected local Markdown prose editing; never uploads.")
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect", help="Extract editable prose without printing embedded images")
    inspect.add_argument("--markdown", required=True)
    inspect.add_argument("--output-spec", default=None)
    inspect.add_argument("--compact", action="store_true", help="Print source prose once; keep protected metadata in the existing specification")
    inspect.add_argument("--read-only", action="store_true", help="Extract prose for analysis without creating a specification")
    apply = commands.add_parser("apply", help="Apply an AI-completed specification to a new verified copy")
    apply.add_argument("--markdown", required=True)
    apply.add_argument("--polish-spec", required=True)
    apply.add_argument("--output", default=None)
    apply.add_argument("--replace", action="append", nargs=2, metavar=("BLOCK_ID", "TEXT"), help="Submit only changed blocks; leave other source prose unchanged")
    apply.add_argument("--polished-by", default=None)
    apply.add_argument("--source-sha256", default=None, help="Required with --replace; exact hash returned by inspect")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            if args.read_only and args.output_spec:
                raise ValueError("Read-only inspection does not accept an output specification")
            result = inspect_markdown_for_polish(args.markdown, output_spec_path=args.output_spec, persist_spec=not args.read_only)
            if args.compact:
                specification = result["specification"]
                result = {
                    "status": result["status"], "source_path": result["source_path"],
                    "polish_spec_path": result["polish_spec_path"], "source_sha256": specification["source_sha256"],
                    "blocks": [{key: block[key] for key in ("id", "heading", "source_text", "required_tokens")} for block in specification["blocks"]],
                }
        else:
            changes = None
            if args.replace is not None:
                if len({block_id for block_id, text in args.replace}) != len(args.replace):
                    raise ValueError("Each block ID must appear only once")
                changes = dict(args.replace)
            elif args.polished_by is not None or args.source_sha256 is not None:
                raise ValueError("Field submission requires --replace")
            result = apply_markdown_polish(
                args.markdown, args.polish_spec, output_path=args.output,
                replacements=changes, polished_by=args.polished_by, expected_source_sha256=args.source_sha256,
            )
    except (PublishError, OSError, ValueError) as error:
        result = {
            "status": "blocked", "code": getattr(error, "code", "invalid_input"),
            "stage": getattr(error, "stage", None) or "input validation", "message": str(error),
            "next_action": "Correct the reported input or protected edits; no upload was attempted.",
        }
        if getattr(error, "details", None):
            result["details"] = error.details
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())