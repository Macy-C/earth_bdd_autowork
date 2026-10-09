from Bdd.page_obj.right_screen.completed.page import CompletedPage
from Bdd.page_obj.right_screen.recon.page import ReconPage
from Bdd.page_obj.right_screen.report.page import ReportPage
from autowork_core.page import WindowPage


class RightScreenPage(WindowPage):
    root_locator_file = "right_screen/right_screen.yaml"
    root_locator = "right_screen_window"

    @property
    def completed(self) -> CompletedPage:
        return self.get_view(CompletedPage)

    @property
    def recon(self) -> ReconPage:
        return self.get_view(ReconPage)

    @property
    def report(self) -> ReportPage:
        return self.get_view(ReportPage)