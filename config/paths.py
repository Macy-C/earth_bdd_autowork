from pathlib import Path


class Paths:
    BASE_DIR = Path(__file__).resolve().parent.parent

    BDD_DIR = BASE_DIR / "Bdd"
    PROJECT_CONFIG_FILE = BDD_DIR / "config.yaml"
    LOCATORS_DIR = BDD_DIR / "locators"
    DATA_DIR = BDD_DIR / "data"
    PAGE_OBJ_DIR = BDD_DIR / "page_obj"

    ARTIFACTS_DIR = BASE_DIR / "artifacts"
    LOGS_DIR = ARTIFACTS_DIR / "logs"
    SCREENSHOTS_DIR = ARTIFACTS_DIR / "screenshots"
    RECORDINGS_DIR = ARTIFACTS_DIR / "recordings"
    REPORTS_DIR = ARTIFACTS_DIR / "reports"

    CORE_DIR = BASE_DIR / "autowork_core"
    UTILS_DIR = CORE_DIR / "utils"
    RESOURCES_DIR = BASE_DIR / "resources"
    MODELS_DIR = RESOURCES_DIR / "models"
    ALLURE_CLI_DIR = RESOURCES_DIR / "allure_cli"
    FFMPEG_DIR = RESOURCES_DIR / "ffmpeg"
    FFMPEG_EXE = FFMPEG_DIR / "current" / "bin" / "ffmpeg.exe"

    FEATURES_DIR = BDD_DIR / "features"
    TEST_FEATURES_DIR = BDD_DIR / "test_features"




