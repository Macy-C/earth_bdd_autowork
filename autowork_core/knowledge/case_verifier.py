from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path


FRAME_BLOCK_PATTERN = re.compile(
    r"(?m)^##### (F\d+) [^\n]+(?:\n[^\n]+\n!\[[^\n]+\]\(resource://OBec3Ido0Vdba121TswHBA\)| !\[[^\n]+\]\(resource://OBec3Ido0Vdba121TswHBA\)(?P<lead_tail>[^\n]+))"
)
STEP_SOURCE_HEADING_PATTERN = re.compile(r"(?m)^#{3,4} \[S\d+-Step\d+\][^\n]+$")
TARGET_ROW_PATTERN = re.compile(r"(?m)^\|\s*(T\d+)\s*\|\s*([^|]+?)\s*\|\s*(Q\d+|-)\s*\|")
QUERY_ROW_PATTERN = re.compile(r"(?m)^\|\s*(Q\d+)\s*\|\s*([^|]+?)\s*\|")
ACTION_ROW_PATTERN = re.compile(r'(?m)^\|\s*<a id="[^"]+"></a>(\d+\.\d+)\s*\(([^)]+)\)\s*\|')
ACTION_OPERATION_PATTERN = re.compile(r'(?m)^\|\s*<a id="[^"]+"></a>(\d+\.\d+)\s*\([^)]+\)\s*\|[^\n|]+\|\s*`([^`]+)`\s*\|')
BASE64_IMAGE_PATTERN = re.compile(r"data:image/png;base64,([A-Za-z0-9+/=]+)")
WEKNORA_WIKI_SUMMARY_LIMIT = 32768
PRE_IMAGE_SAFE_BUDGET = 30000


def verify_case_markdown(
    markdown_path,
    *,
    observations_path=None,
    max_frame_chars=380,
    max_pre_image_chars=WEKNORA_WIKI_SUMMARY_LIMIT,
):
    """Deterministically verify a case Markdown file for WeKnora v0.8.3 Wiki readiness."""
    path = Path(markdown_path).resolve()
    text = path.read_text(encoding="utf-8-sig")
    errors = []
    warnings = []
    from autowork_core.knowledge.case_export import case_contract

    for chapter in range(1, 8):
        if not re.search(rf"(?m)^## {chapter}\.\s+\S", text):
            errors.append(f"Missing case chapter {chapter}")
    try:
        _references, declarations = case_contract(text)
        if len(declarations) != 1:
            errors.append(f"The case must declare exactly one Scenario; found {len(declarations)}")
    except Exception as error:
        errors.append(f"Invalid declaration/source contract: {error}")

    raw_b64_list = BASE64_IMAGE_PATTERN.findall(text)
    image_digests = []
    for idx, encoded in enumerate(raw_b64_list, 1):
        try:
            payload = _decode_png(encoded)
            image_digests.append(hashlib.sha256(payload).hexdigest())
        except Exception as err:
            errors.append(f"Image #{idx} invalid Base64/PNG: {err}")

    clean = BASE64_IMAGE_PATTERN.sub("resource://OBec3Ido0Vdba121TswHBA", text)
    frame_matches = list(FRAME_BLOCK_PATTERN.finditer(clean))
    max_observed_len = max((len(m.group(0)) for m in frame_matches), default=0)
    over_limit = [
        {"frame": m.group(1), "length": len(m.group(0))}
        for m in frame_matches
        if len(m.group(0)) > max_frame_chars
    ]
    if over_limit:
        errors.append(f"Frame blocks exceed {max_frame_chars} chars: {over_limit}")

    if len(frame_matches) != len(raw_b64_list):
        errors.append(
            f"Co-located frame block count ({len(frame_matches)}) != embedded PNG count ({len(raw_b64_list)})"
        )
    for m in frame_matches:
        lead_tail = m.group("lead_tail")
        if lead_tail is not None:
            if len(lead_tail) < 165:
                errors.append(f"Frame block {m.group(1)} trailing guard ({len(lead_tail)} chars) < 165 chars")
            tail_without_entities = re.sub(r"&#\d+;|&[a-zA-Z]+;", "X", lead_tail)
            if any(sep in tail_without_entities for sep in (";", "；", "。", "！", "？")):
                errors.append(f"Frame block {m.group(1)} trailing guard contains chunk splitter punctuation")

    for step_heading in STEP_SOURCE_HEADING_PATTERN.findall(clean):
        if "所属需求：" not in step_heading or "所属业务场景：" not in step_heading:
            errors.append(f"Step source heading missing Feature/Scenario attribution: {step_heading!r}")
        if "所属 Page/View：" in step_heading:
            errors.append(f"Step source heading must not route to Page/View entity pages: {step_heading!r}")

    first_img_match = re.search(r"!\[F\d+\]\(resource://OBec3Ido0Vdba121TswHBA\)", clean)
    if first_img_match:
        pre_image_chars = first_img_match.start()
    elif "\n## 4." in clean:
        pre_image_chars = clean.index("\n## 4.")
    else:
        pre_image_chars = len(clean)
    if pre_image_chars > max_pre_image_chars:
        warnings.append(
            f"前置摘要区（{pre_image_chars} 字符）已超过 WeKnora 摘要截断窗口（{max_pre_image_chars} 字符），"
            "第 3 章末尾部分概览内容可能不会进入 Summary 页（Entity/Concept 页不受影响）。"
        )
    elif pre_image_chars > PRE_IMAGE_SAFE_BUDGET:
        warnings.append(
            f"前置摘要区（{pre_image_chars} 字符）接近 WeKnora 摘要截断窗口（{max_pre_image_chars} 字符），建议关注场景规模。"
        )

    prose_region = _extract_prose_region(clean)
    bare_quotes = prose_region.count('"')
    if bare_quotes > 0:
        pos = prose_region.find('"')
        snippet = prose_region[max(0, pos - 40): min(len(prose_region), pos + 40)]
        errors.append(f"Bare double quotes ({bare_quotes}) found in prose before Trace Index: {snippet!r}")

    target_rows = TARGET_ROW_PATTERN.findall(clean)
    for tid, scope_col, _ in target_rows:
        if "Step " not in scope_col:
            errors.append(f"Target row {tid} missing Step attribution: {scope_col!r}")

    query_rows = QUERY_ROW_PATTERN.findall(clean)
    for qid, scope_col in query_rows:
        if "Step " not in scope_col:
            errors.append(f"Query row {qid} missing Step attribution: {scope_col!r}")

    action_rows = ACTION_ROW_PATTERN.findall(clean)
    if not action_rows:
        errors.append("The case must contain an explicit action trace")
    if len({identity for identity, operation in action_rows}) != len(action_rows):
        errors.append("Action trace contains duplicate Step.Action identities")
    if not observations_path:
        warnings.append("No observations supplied: action values and image completeness are not independently verified")

    obs_check = None
    if observations_path:
        obs_file = Path(observations_path).resolve()
        if obs_file.is_file():
            obs = json.loads(obs_file.read_text(encoding="utf-8-sig"))
            header = text.split("\n## 2.", 1)[0]
            source_match = re.search(r"Feature 身份[^`\r\n]*`([^`]+)`", header)
            scenario_match = re.search(r"Scenario：`([^`]+)`", header)
            features = [feature for feature in obs.get("features", ()) if source_match and feature.get("source") == source_match.group(1)]
            scenarios = [scenario for feature in features for scenario in feature.get("scenarios", ()) if scenario_match and scenario.get("name") == scenario_match.group(1)]
            if len(features) != 1 or len(scenarios) != 1:
                errors.append("The document must identify exactly one captured Feature/Scenario")
            expected_actions = [
                act
                for scen in scenarios
                for step in scen.get("steps", ())
                for act in step.get("actions", ())
            ]
            expected_digests = list(dict.fromkeys(
                img["sha256"]
                for act in expected_actions
                for img in act.get("images", ())
                if img.get("status") == "captured" and img.get("sha256")
            ))
            if len(action_rows) != len(expected_actions):
                errors.append(
                    f"Action row count ({len(action_rows)}) != observations.json ({len(expected_actions)})"
                )
            expected_trace = [
                (f"{step_index}.{action_index}", action["operation"])
                for scenario in scenarios
                for step_index, step in enumerate(scenario.get("steps", ()), 1)
                for action_index, action in enumerate(step.get("actions", ()), 1)
            ]
            if ACTION_OPERATION_PATTERN.findall(clean) != expected_trace:
                errors.append("Step.Action identities or operation order differ from captured evidence")
            if image_digests != expected_digests:
                errors.append("Embedded PNG SHA256 sequence does not match observations.json")
            obs_check = {
                "expected_actions": len(expected_actions),
                "expected_unique_pngs": len(expected_digests),
            }
        else:
            errors.append("The requested observations.json does not exist")

    return {
        "status": "passed" if not errors else "invalid",
        "markdown_path": str(path),
        "summary": {
            "pre_image_chars": pre_image_chars,
            "pre_image_budget": max_pre_image_chars,
            "pre_image_within_budget": pre_image_chars <= max_pre_image_chars,
            "embedded_pngs": len(raw_b64_list),
            "colocated_frame_blocks": len(frame_matches),
            "max_frame_block_chars": max_observed_len,
            "bare_quotes_in_prose": bare_quotes,
            "action_trace_rows": len(action_rows),
            "target_index_rows": len(target_rows),
            "query_index_rows": len(query_rows),
            "observations_comparison": obs_check,
        },
        "errors": errors,
        "warnings": warnings,
    }


def _decode_png(encoded):
    from io import BytesIO
    from PIL import Image

    payload = base64.b64decode(encoded, validate=True)
    with Image.open(BytesIO(payload)) as img:
        if img.format != "PNG":
            raise ValueError(f"Expected PNG, got {img.format}")
        img.verify()
    return payload


def _extract_prose_region(clean_text):
    if "## 8. Current Run Scope and Observations" in clean_text:
        region = clean_text.split("## 8. Current Run Scope and Observations", 1)[1]
        if "### 8.1 精确追溯索引" in region:
            region = region.split("### 8.1 精确追溯索引", 1)[0]
    elif "## 3. 测试方法" in clean_text and "## 5. 精确追溯索引" in clean_text:
        region = clean_text.split("## 3. 测试方法", 1)[1].split("## 5. 精确追溯索引", 1)[0]
    else:
        region = clean_text
    region = re.sub(r"```.*?```", "", region, flags=re.S)
    region = re.sub(r'<a id="[^"]+"></a>', "", region)
    return region
