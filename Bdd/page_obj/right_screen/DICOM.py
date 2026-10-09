from Bdd.page_obj.right_screen.IV.page import IVPage
from Bdd.page_obj.right_screen.analysis.page import AnalysisPage
from Bdd.page_obj.right_screen.review.page import ReviewPage
from autowork_core.page import WindowPage


class DICOMPage(WindowPage):
    root_locator_file = "right_screen/DICOM.yaml"
    root_locator = "DICOM_window"

    @property
    def analysis(self) -> AnalysisPage:
        return self.get_view(AnalysisPage)

    @property
    def IV(self) -> IVPage:
        return self.get_view(IVPage)

    @property
    def review(self) -> ReviewPage:
        return self.get_view(ReviewPage)