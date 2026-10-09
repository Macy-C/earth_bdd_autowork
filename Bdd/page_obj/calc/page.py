from autowork_core.page import WindowPage


class CalcPage(WindowPage):
    root_locator_file = "calc/right_screen.yaml"
    root_locator = "calc_window"

    # @property
    # def demo(self) -> DemoView:  WindowView
    #     return self.get_view(DemoView)