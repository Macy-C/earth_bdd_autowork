from Bdd.page_obj.left_screen.patient.page import NewPatientPage
from Bdd.page_obj.left_screen.scheduled.add_patient import AddPatientPage
from Bdd.page_obj.left_screen.scheduled.page import ScheduledPage
from Bdd.page_obj.left_screen.service.QA import QAPage
from Bdd.page_obj.left_screen.service.audit_trail import AuditTrailPage
from Bdd.page_obj.left_screen.service.bug_reports import BugReportsPage
from Bdd.page_obj.left_screen.service.dose_check_report import DoseCheckReportPage
from Bdd.page_obj.left_screen.service.exam_card_manager import ExamCardManagerPage
from Bdd.page_obj.left_screen.service.page import Service
from Bdd.page_obj.left_screen.service.short_tube_conditioning import ShortTubeConditioningPage
from Bdd.page_obj.left_screen.service.system_setting import SystemSettingPage
from autowork_core.page import WindowPage


class LeftScreenPage(WindowPage):
    root_locator_file = "left_screen/right_screen.yaml"
    root_locator = "left_screen_window"

    @property
    def patient(self) -> NewPatientPage:
        return self.get_view(NewPatientPage)

    #--------------------------------------------------
    @property
    def scheduled(self) -> ScheduledPage:
        return self.get_view(ScheduledPage)

    @property
    def scheduled_add_patient(self) -> AddPatientPage:
        return self.get_view(AddPatientPage)

    #--------------------------------------------------
    @property
    def service(self) -> Service:
        return self.get_view(Service)

    @property
    def service_audit_trail(self) -> AuditTrailPage:
        return self.get_view(AuditTrailPage)

    @property
    def service_bug_reports(self) -> BugReportsPage:
        return self.get_view(BugReportsPage)

    @property
    def service_dose_check_report(self) -> DoseCheckReportPage:
        return self.get_view(DoseCheckReportPage)

    @property
    def service_exam_card_manager(self) -> ExamCardManagerPage:
        return self.get_view(ExamCardManagerPage)

    @property
    def service_QA(self) -> QAPage:
        return self.get_view(QAPage)

    @property
    def service_short_tube_conditioning(self) -> ShortTubeConditioningPage:
        return self.get_view(ShortTubeConditioningPage)

    @property
    def service_system_setting(self) -> SystemSettingPage:
        return self.get_view(SystemSettingPage)

    #--------------------------------------------------





















