class UnresolvedGenerationIssue(RuntimeError):
    def __init__(self, issue_id, *, step_id, issue_type):
        self.issue_id = str(issue_id)
        self.step_id = str(step_id)
        self.issue_type = str(issue_type)
        super().__init__(
            "Generated Step has an unresolved issue: "
            f"{self.issue_id} ({self.issue_type}, step={self.step_id})"
        )


def unresolved_generation_issue(issue_id, *, step_id, issue_type):
    raise UnresolvedGenerationIssue(
        issue_id,
        step_id=step_id,
        issue_type=issue_type,
    )


__all__ = ["UnresolvedGenerationIssue", "unresolved_generation_issue"]