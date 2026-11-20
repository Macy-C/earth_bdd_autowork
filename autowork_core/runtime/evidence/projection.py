import os
import re
from pathlib import Path

from config.paths import Paths
from autowork_core.runtime.status import status_category


def duration(seconds):
    if seconds is None:
        return "-"
    minutes, seconds = divmod(int(max(0, seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def failure_kind(raw):
    return {"failed": "assertion", "hook_error": "hook", "cleanup_error": "cleanup",
            "undefined": "definition", "pending": "definition", "error": "automation"}.get(raw)


def aggregate(items):
    statuses = [status_category(item.get("status", "unknown")) for item in items]
    if "failed" in statuses:
        return "failed"
    if statuses and all(item == "passed" for item in statuses):
        return "passed"
    if statuses and all(item == "skipped" for item in statuses):
        return "skipped"
    return "unknown"


def report_projection(snapshot):
    project = Path(Paths.BASE_DIR).resolve()

    def attachments(records):
        result = []
        for item in records:
            path = Path(item["path"])
            if not path.is_absolute():
                path = project / path
            result.append(dict(item, path=os.path.relpath(path, Paths.REPORTS_DIR).replace("\\", "/")))
        return result

    def logs_for(node):
        identity = node.get("id", snapshot["execution_id"])
        return [item for item in snapshot.get("logs", ())
                if item["scope_id"] == identity or item["scope_id"].startswith(identity + "/")]

    def step_projection(step):
        raw = step.get("rawStatus", step.get("test_status", "unknown"))
        result = {"keyword": step["keyword"], "name": step["name"],
                  "status": step.get("status", "unknown"), "rawStatus": raw,
                  "duration": duration(step.get("duration_seconds")),
                  "attachments": attachments(list(step.get("attachments", ())) + [
                      item for action in step.get("actions", ()) for item in action.get("attachments", ())
                  ]),
                  "evidenceId": step["id"], "actions": step.get("actions", []),
                  "logs": logs_for(step), "lifecycleErrors": step.get("exceptions", [])}
        for key in ("error", "errorDetail", "diagnostic"):
            if key in step:
                result[key] = step[key]
        if step.get("screenshot_error"):
            result["errorDetail"] = (result.get("errorDetail", "") + "\n失败截图保存失败: " + step["screenshot_error"]).strip()
        kind = failure_kind(raw)
        if kind:
            result["failureKind"] = kind
        return result

    def scenario_projection(scenario):
        raw = scenario.get("rawStatus", scenario.get("test_status", "unknown"))
        result = {"type": "scenario", "name": scenario["name"],
                  "status": scenario.get("status", "unknown"), "rawStatus": raw,
                  "duration": duration(scenario.get("duration_seconds")), "tags": scenario.get("tags", []),
                  "owner": "", "steps": [step_projection(step) for step in scenario["steps"]],
                  "attachments": attachments(scenario.get("attachments", ())),
                  "evidenceId": scenario["id"], "executionId": snapshot["execution_id"],
                  "lifecycleErrors": scenario.get("exceptions", []),
                  "stepScope": {"files": scenario.get("scope", {}).get("files", []),
                                "entryFile": scenario.get("scope", {}).get("entry_file"),
                                "origin": scenario.get("scope", {}).get("origin"),
                                "fingerprint": scenario.get("scope", {}).get("fingerprint"),
                                "declarations": scenario.get("scope", {}).get("declarations", [])}}
        for key in ("skipScope", "skipReason"):
            if key in scenario:
                result[key] = scenario[key]
        kind = failure_kind(raw)
        if kind:
            result["failureKind"] = kind
        parent = scenario.get("parent") or {}
        row = scenario.get("example")
        match = re.match(r"^(.+?)\s+--\s+@(\d+(?:\.\d+)?)(?:\s+.*)?$", scenario["name"])
        if row is not None or match:
            result.update(type="example", outlineName=parent.get("name") or match.group(1),
                          exampleId=str(row.get("id") if row else match.group(2)),
                          params=dict(zip(row["headings"], row["cells"])) if row else {})
        return result

    features = []
    for feature in snapshot["features"]:
        projected = {"name": feature["name"], "file": feature["source"],
                     "tags": feature.get("tags", []), "rawStatus": feature.get("rawStatus", "unknown"),
                     "evidenceId": feature["id"], "scenarios": []}
        if feature.get("skipScope") == "feature":
            projected.update({key: feature[key] for key in ("skipScope", "skipReason", "plannedUnits", "plannedSteps")})
            projected["status"] = "skipped"
        else:
            outlines = {}
            units = [scenario_projection(item) for item in feature["scenarios"]]
            for scenario in units:
                if scenario["type"] != "example":
                    projected["scenarios"].append(scenario)
                    continue
                name = scenario["outlineName"]
                if name not in outlines:
                    outlines[name] = {"type": "outline", "name": name, "tags": scenario["tags"], "owner": "", "examples": []}
                    projected["scenarios"].append(outlines[name])
                outlines[name]["examples"].append(scenario)
            for outline in outlines.values():
                outline["status"] = aggregate(outline["examples"])
                outline["duration"] = duration(sum(
                    item.get("duration_seconds", 0) for item in feature["scenarios"]
                    if (item.get("parent") or {}).get("name") == outline["name"]
                ))
            projected["status"] = aggregate(units)
        kind = failure_kind(projected["rawStatus"])
        if kind:
            projected["failureKind"] = kind
        features.append(projected)
    return {"title": "桌面自动化测试报告", "project": "BDD Autowork",
            "startedAt": snapshot["started_at"], "duration": duration(snapshot.get("duration_seconds")),
            "features": features, "executionSummary": {
                "executionId": snapshot["execution_id"], "outcome": snapshot["runner_outcome"],
                "captureStatus": snapshot["capture_status"], "state": snapshot["snapshot_state"],
                "exceptions": snapshot.get("exceptions", []), "errors": snapshot.get("errors", []),
                "coverage": snapshot.get("coverage", {})}}