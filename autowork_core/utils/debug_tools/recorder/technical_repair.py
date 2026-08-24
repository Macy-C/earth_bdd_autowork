from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from autowork_core.utils.debug_tools.recorder.identity import stable_digest
from autowork_core.utils.debug_tools.recorder.request_repository import (
    request_fingerprint,
    request_identity_is_valid,
    request_matches_current_projection,
    request_revision_matches,
    resolve_session_path,
    session_dir_for_request_path,
)
from autowork_core.utils.debug_tools.recorder.run_lock import RunWriteLock
from autowork_core.utils.debug_tools.recorder.target_repair import (
    TargetRepairService,
    _refresh_session_after_repair,
    _run_directory,
)
from autowork_core.utils.debug_tools.recorder.timeline import TimelineStore
from autowork_core.utils.debug_tools.recorder.writer import write_json_atomic


TECHNICAL_REPAIR_PACK_VERSION = "1.0"
TECHNICAL_REPAIR_PROPOSAL_VERSION = "1.0"
TECHNICAL_REPAIR_RECEIPT_VERSION = "1.0"
_PROPOSAL_FIELDS = {
    "technical_repair_proposal_version",
    "request_id",
    "repair_pack_id",
    "repair_pack_fingerprint",
    "revision_seal",
    "proposals",
}
_ITEM_FIELDS = {
    "issue_id",
    "repair_kind",
    "action_id",
    "selected_candidate_id",
    "reason",
    "confidence",
}


class TechnicalRepairService:
    """Materialize a system-verified target binding selection."""

    def __init__(self, take_dir, *, artifact_root=None):
        self.take_dir = Path(take_dir).resolve()
        self.timeline = TimelineStore(self.take_dir)
        self.target_repair = TargetRepairService(self.take_dir)
        self.artifact_root = (
            Path(artifact_root).resolve()
            if artifact_root is not None
            else self.take_dir / "technical-repairs"
        )

    def build_pack(
            self,
            *,
            request_id,
            revision_seal,
            step_id,
            action_id,
            request_fingerprint_value=None,
            take_path=None,
        ):
        candidates = self.target_repair.candidates(action_id)
        pack = {
            "technical_repair_pack_version": TECHNICAL_REPAIR_PACK_VERSION,
            "request_id": str(request_id),
            "revision_seal": str(revision_seal),
            "timeline_revision": self.timeline.current_revision(),
            "issues": [{
                "issue_id": "technical-repair-" + stable_digest(
                    request_id,
                    revision_seal,
                    step_id,
                    action_id,
                    length=16,
                ),
                "repair_kind": "target_binding",
                "step_id": str(step_id),
                "action_id": str(action_id),
                "candidates": [
                    _candidate_summary(candidate)
                    for candidate in candidates
                ],
            }],
            "route": _repair_route(candidates),
        }
        if request_fingerprint_value:
            pack["request_fingerprint"] = str(request_fingerprint_value)
        if take_path:
            pack["take_path"] = str(take_path)
        pack["repair_pack_fingerprint"] = _fingerprint(pack)
        pack["repair_pack_id"] = (
            "repair-pack-" + pack["repair_pack_fingerprint"][:16]
        )
        self._persist("packs", pack["repair_pack_id"], pack)
        return pack

    def apply_proposal(
            self,
            proposal,
            *,
            expected_revision,
            receipt_context=None,
            persist_receipt=True,
        ):
        proposal = _validate_proposal(proposal)
        pack = self._load_pack(proposal)
        if proposal["revision_seal"] != pack["revision_seal"]:
            raise ValueError("technical repair proposal revision不一致")
        if expected_revision != pack["timeline_revision"]:
            raise ValueError("technical repair proposal timeline revision已变化")
        if self.timeline.current_revision() != expected_revision:
            raise ValueError("technical repair proposal timeline revision已变化")
        selected = _resolve_selection(pack, proposal)
        run_directory = _run_directory(self.take_dir)
        lock = RunWriteLock(run_directory).acquire()
        try:
            self.timeline.require_revision(expected_revision)
            candidate = _load_current_candidate(
                self.target_repair,
                selected["action_id"],
                selected["selected_candidate_id"],
            )
            state = self.timeline.apply_target_binding_repair(
                candidate,
                expected_revision=expected_revision,
            )
            _refresh_session_after_repair(run_directory, self.take_dir)
        finally:
            lock.release()
        receipt = {
            "technical_repair_receipt_version": TECHNICAL_REPAIR_RECEIPT_VERSION,
            "request_id": proposal["request_id"],
            "repair_pack_id": pack["repair_pack_id"],
            "repair_pack_fingerprint": pack["repair_pack_fingerprint"],
            "proposal_fingerprint": _fingerprint(proposal),
            "revision_seal": proposal["revision_seal"],
            "repair_kind": selected["repair_kind"],
            "issue_id": selected["issue_id"],
            "action_id": selected["action_id"],
            "candidate_id": candidate["candidate_id"],
            "candidate_fingerprint": _fingerprint(candidate),
            "pre_timeline_revision": expected_revision,
            "post_timeline_revision": state["timeline_revision"],
            "raw_evidence_modified": False,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        receipt.update(dict(receipt_context or {}))
        receipt["receipt_fingerprint"] = _fingerprint(receipt)
        receipt["receipt_id"] = (
            "technical-repair-" + receipt["receipt_fingerprint"][:16]
        )
        if persist_receipt:
            self._persist("receipts", receipt["receipt_id"], receipt)
        return {"status": "applied", "receipt": receipt}

    def _load_pack(self, proposal):
        path = self._directory("packs") / f"{proposal['repair_pack_id']}.json"
        pack = _read_json_required(path)
        expected = _fingerprint({
            key: value
            for key, value in pack.items()
            if key not in {"repair_pack_fingerprint", "repair_pack_id"}
        })
        if any((
            pack.get("technical_repair_pack_version")
            != TECHNICAL_REPAIR_PACK_VERSION,
            pack.get("repair_pack_id") != proposal["repair_pack_id"],
            pack.get("repair_pack_fingerprint")
            != proposal["repair_pack_fingerprint"],
            pack.get("repair_pack_fingerprint") != expected,
        )):
            raise ValueError("technical repair pack完整性无效")
        return pack

    def _persist(self, category, identifier, value):
        path = self._directory(category) / f"{identifier}.json"
        if path.exists():
            if _read_json_required(path) != value:
                raise ValueError("technical repair artifact ID冲突")
            return path
        write_json_atomic(path, value)
        return path

    def _directory(self, category):
        path = self.artifact_root / category
        path.mkdir(parents=True, exist_ok=True)
        return path


class RequestTechnicalRepairService:
    """Expose technical repair through a current immutable RequestV3 only."""

    def __init__(self, request_path):
        self.request_path = Path(request_path).resolve()
        self.request = _read_json_required(self.request_path)
        self.session_dir = session_dir_for_request_path(
            self.request_path,
            self.request,
        )

    def build_pack(self, *, step_id, action_id):
        self._require_current_request()
        take_dir = self._take_for_step(step_id)
        return TechnicalRepairService(
            take_dir,
            artifact_root=self._artifact_root(),
        ).build_pack(
            request_id=self.request["request_id"],
            revision_seal=(self.request.get("revision_snapshot") or {}).get(
                "seal"
            ),
            step_id=step_id,
            action_id=action_id,
            request_fingerprint_value=request_fingerprint(self.request),
            take_path=take_dir.relative_to(self.session_dir).as_posix(),
        )

    def apply_proposal(self, proposal):
        proposal = _validate_proposal(proposal)
        self._require_current_request()
        if proposal["request_id"] != self.request.get("request_id"):
            raise ValueError("technical repair proposal不属于当前Request")
        pack = self._load_pack(proposal)
        self._validate_pack_binding(pack)
        _resolve_selection(pack, proposal)
        proposal_record = self._persist_proposal(proposal)
        return self._apply_selection(
            proposal,
            pack,
            selection_source="ai_select",
            proposal_record=proposal_record,
        )

    def apply_auto_fix(self, *, step_id, action_id):
        pack = self.build_pack(step_id=step_id, action_id=action_id)
        if pack["route"] != "auto_fix":
            raise ValueError(
                "technical repair不是唯一系统候选，不能自动修复"
            )
        issue = pack["issues"][0]
        proposal = {
            "technical_repair_proposal_version": (
                TECHNICAL_REPAIR_PROPOSAL_VERSION
            ),
            "request_id": self.request["request_id"],
            "repair_pack_id": pack["repair_pack_id"],
            "repair_pack_fingerprint": pack["repair_pack_fingerprint"],
            "revision_seal": pack["revision_seal"],
            "proposals": [{
                "issue_id": issue["issue_id"],
                "repair_kind": "target_binding",
                "action_id": action_id,
                "selected_candidate_id": issue["candidates"][0][
                    "candidate_id"
                ],
                "reason": "Unique system-verified technical candidate.",
                "confidence": "high",
            }],
        }
        return self._apply_selection(
            proposal,
            pack,
            selection_source="system_auto",
            proposal_record=None,
        )

    def _apply_selection(
            self,
            proposal,
            pack,
            *,
            selection_source,
            proposal_record,
        ):
        take_dir = resolve_session_path(self.session_dir, pack["take_path"])
        result = TechnicalRepairService(
            take_dir,
            artifact_root=self._artifact_root(),
        ).apply_proposal(
            proposal,
            expected_revision=pack["timeline_revision"],
            receipt_context={
                "request_fingerprint": request_fingerprint(self.request),
                "request_path": self.request_path.relative_to(
                    self.session_dir
                ).as_posix(),
                "step_id": pack["issues"][0]["step_id"],
                "take_path": pack["take_path"],
                "selection_source": selection_source,
                "proposal_id": (
                    proposal_record.get("proposal_id")
                    if proposal_record is not None
                    else None
                ),
            },
            persist_receipt=False,
        )
        if request_matches_current_projection(self.session_dir, self.request):
            raise RuntimeError("technical repair未使旧Request失效")
        receipt = result["receipt"]
        receipt["request_stale"] = True
        receipt["receipt_fingerprint"] = _fingerprint({
            key: value
            for key, value in receipt.items()
            if key not in {"receipt_fingerprint", "receipt_id"}
        })
        receipt["receipt_id"] = (
            "technical-repair-" + receipt["receipt_fingerprint"][:16]
        )
        self._persist_receipt(receipt)
        return {"status": "applied", "receipt": receipt}

    def _require_current_request(self):
        if not request_identity_is_valid(self.request):
            raise ValueError("technical repair需要有效RequestV3")
        if not request_matches_current_projection(
                self.session_dir,
                self.request,
        ):
            raise ValueError("technical repair Request revision已变化")
        matches, _current = request_revision_matches(
            self.session_dir,
            self.request,
            self.request.get("revision_snapshot"),
        )
        if not matches:
            raise ValueError("technical repair Request revision已变化")

    def _take_for_step(self, step_id):
        matches = [
            evidence
            for evidence in self.request.get("evidence") or ()
            if str((evidence.get("step") or {}).get("id") or "")
            == str(step_id)
        ]
        if len(matches) != 1:
            raise ValueError("technical repair Step不属于当前Request")
        return resolve_session_path(
            self.session_dir,
            (matches[0].get("artifacts") or {}).get("take"),
        )

    def _load_pack(self, proposal):
        path = self._artifact_root() / "packs" / (
            f"{proposal['repair_pack_id']}.json"
        )
        pack = _read_json_required(path)
        expected = _fingerprint({
            key: value
            for key, value in pack.items()
            if key not in {"repair_pack_fingerprint", "repair_pack_id"}
        })
        if any((
            pack.get("repair_pack_fingerprint") != expected,
            pack.get("repair_pack_id") != proposal["repair_pack_id"],
            pack.get("repair_pack_fingerprint")
            != proposal["repair_pack_fingerprint"],
        )):
            raise ValueError("technical repair pack完整性无效")
        return pack

    def _validate_pack_binding(self, pack):
        if any((
            pack.get("request_id") != self.request.get("request_id"),
            pack.get("request_fingerprint") != request_fingerprint(self.request),
            pack.get("revision_seal")
            != (self.request.get("revision_snapshot") or {}).get("seal"),
        )):
            raise ValueError("technical repair pack不属于当前Request revision")
        issue = (pack.get("issues") or [{}])[0]
        expected = self._take_for_step(issue.get("step_id"))
        if pack.get("take_path") != expected.relative_to(
                self.session_dir
        ).as_posix():
            raise ValueError("technical repair pack Take不属于当前Request")

    def _artifact_root(self):
        return (
            self.session_dir / "ai" / "technical-repairs"
            / self.request["request_id"]
        )

    def _persist_proposal(self, proposal):
        fingerprint = _fingerprint(proposal)
        record = {
            "technical_repair_proposal_version": (
                TECHNICAL_REPAIR_PROPOSAL_VERSION
            ),
            "proposal_id": "technical-repair-proposal-" + fingerprint[:16],
            "proposal_fingerprint": fingerprint,
            "proposal": proposal,
        }
        path = self._artifact_root() / "proposals" / (
            f"{record['proposal_id']}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if _read_json_required(path) != record:
                raise ValueError("technical repair proposal ID冲突")
            return record
        write_json_atomic(path, record)
        return record

    def _persist_receipt(self, receipt):
        path = self._artifact_root() / "receipts" / (
            f"{receipt['receipt_id']}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if _read_json_required(path) != receipt:
                raise ValueError("technical repair receipt ID冲突")
            return path
        write_json_atomic(path, receipt)
        return path


def _candidate_summary(candidate):
    element = candidate.get("element") or {}
    return {
        "candidate_id": candidate["candidate_id"],
        "auto_id": element.get("auto_id"),
        "control_type": element.get("control_type"),
        "name": element.get("name"),
        "candidate_fingerprint": _fingerprint(candidate),
    }


def _validate_proposal(value):
    proposal = dict(value or {})
    unexpected = sorted(set(proposal) - _PROPOSAL_FIELDS)
    if unexpected:
        raise ValueError(f"technical repair proposal不允许字段: {unexpected}")
    if any((
        proposal.get("technical_repair_proposal_version")
        != TECHNICAL_REPAIR_PROPOSAL_VERSION,
        not proposal.get("request_id"),
        not proposal.get("repair_pack_id"),
        not proposal.get("repair_pack_fingerprint"),
        not proposal.get("revision_seal"),
        not isinstance(proposal.get("proposals"), list),
        len(proposal.get("proposals") or ()) != 1,
    )):
        raise ValueError("technical repair proposal无效")
    item = proposal["proposals"][0]
    if not isinstance(item, dict):
        raise ValueError("technical repair proposal无效")
    unexpected = sorted(set(item) - _ITEM_FIELDS)
    if unexpected:
        raise ValueError(f"technical repair proposal不允许字段: {unexpected}")
    if any((
        item.get("repair_kind") != "target_binding",
        not item.get("issue_id"),
        not item.get("action_id"),
        not item.get("selected_candidate_id"),
        not isinstance(item.get("reason"), str),
        item.get("confidence") not in {"high", "medium", "low"},
    )):
        raise ValueError("technical repair proposal无效")
    return proposal


def _resolve_selection(pack, proposal):
    item = proposal["proposals"][0]
    issue = next((
        value for value in pack.get("issues") or ()
        if value.get("issue_id") == item["issue_id"]
    ), None)
    if issue is None or any((
        issue.get("repair_kind") != item["repair_kind"],
        issue.get("action_id") != item["action_id"],
        item["selected_candidate_id"] not in {
            candidate.get("candidate_id")
            for candidate in issue.get("candidates") or ()
        },
    )):
        raise ValueError("technical repair proposal未选择冻结候选")
    return item


def _load_current_candidate(service, action_id, candidate_id):
    candidate = next((
        value for value in service.candidates(action_id)
        if value.get("candidate_id") == candidate_id
    ), None)
    if candidate is None:
        raise ValueError("technical repair candidate已变化")
    return candidate


def _repair_route(candidates):
    if len(candidates) == 1:
        return "auto_fix"
    if candidates:
        return "ai_select"
    return "unresolved"


def _fingerprint(value):
    return hashlib.sha256(json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _read_json_required(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("technical repair artifact不可读取") from error
    if not isinstance(value, dict):
        raise ValueError("technical repair artifact无效")
    return value
