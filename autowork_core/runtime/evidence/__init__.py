"""Run-scoped evidence collection, independent of report and AI consumers."""

__all__ = ["RunEvidenceCollector"]


def __getattr__(name):
	if name == "RunEvidenceCollector":
		from .collector import RunEvidenceCollector
		return RunEvidenceCollector
	raise AttributeError(name)