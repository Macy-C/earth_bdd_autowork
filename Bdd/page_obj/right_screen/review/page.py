from autowork_core.page import WindowView


class ReviewPage(WindowView):
    locator_files = ["right_screen/review/window.yaml",
                     "right_screen/review/review_2D.yaml",
                     "right_screen/review/review_Endo.yaml",
                     "right_screen/review/review_MPR.yaml",
                     "right_screen/review/review_Volume.yaml"]
    active_locator = None
