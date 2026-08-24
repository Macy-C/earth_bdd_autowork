from pathlib import Path


FRAMEWORK_AI_ROOT = Path("ai")
PROJECT_AI_ROOT = Path("Bdd/ai")

PROJECT_KNOWLEDGE_ROOT = PROJECT_AI_ROOT / "knowledge"
PROJECT_PORTABLE_KNOWLEDGE_ROOT = PROJECT_AI_ROOT / "portable-knowledge"

PORTABLE_KNOWLEDGE_SCHEMA_PATH = (
    FRAMEWORK_AI_ROOT / "context/portable-knowledge.schema.json"
)