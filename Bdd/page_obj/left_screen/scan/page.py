from autowork_core.page import WindowView


class ScanPage(WindowView):
    locator_files = ["left_screen/scan/window.yaml",
                     "left_screen/scan/general.yaml",
                     "left_screen/scan/geometry.yaml",
                     "left_screen/scan/dose_management.yaml",]
    active_locator = None
