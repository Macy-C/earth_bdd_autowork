from Bdd.page_obj.right_screen.filming.page import FilmingPage
from autowork_core.page import WindowPage


class RootFilmingPage(WindowPage):
    root_locator_file = "right_screen/filming.yaml"
    root_locator = "Filming_window"

    @property
    def patient(self) -> FilmingPage:
        return self.get_view(FilmingPage)