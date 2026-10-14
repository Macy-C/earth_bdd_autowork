from time import sleep

from behave import then, when

from Bdd.page_obj.earth_platform_simulator.page import EarthPlatformSimulatorPage
from Bdd.page_obj.left_screen.page import LeftScreenPage
from Bdd.page_obj.right_screen.right_screen import RightScreenPage
from autowork_core.page import get_page
from autowork_core.utils.bus import random_num_and_str, random_str_ABC


@when("a patient is registered with the following information:")
def step_register_patient(context):
    """Patient registration fields are supplied through context.table."""

    patient = get_page(context, LeftScreenPage).patient
    row = context.table.rows[0]
    patient_id = row["Patient ID"]
    patient.set_variable('patient_id',patient_id)
    last_name = row["Last Name"]
    scan_type = row["scan type"]

    patient.input_text('$loc:PatientId_Edit',patient_id)
    patient.input_text('$loc:LastName_Edit',last_name + '_' + random_num_and_str(5))
    patient.input_text('$loc:FirstName_Edit','test_' + random_str_ABC(5))
    patient.click('$loc:Adult_button')
    patient.click('$loc:Reference_button')
    patient.click('$loc:Head_button')
    patient.click('$loc:Head_BrainHelical_protocol')
    patient.click('$loc:Supine_Prone_button')
    patient.click('$loc:posture_option1')
    patient.click('$loc:Start_Exam_button')


@when('a helical scan is completed and "Repeat" is performed')
def step_complete_helical_scan_and_repeat(context):
    simulator = get_page(context, EarthPlatformSimulatorPage)
    scan = get_page(context, LeftScreenPage).scan

    scan.click('$loc:protocol2_option')
    scan.click('$loc:ShowAll_button')

    length = scan.get_attr("$loc:geometry_Length_Edit", 'value.value')
    collimation_list = scan.get_attr("$loc:general_Collimation_Combobox", 'selection')
    collimation = collimation_list[0]
    # cycles = scan.get_attr("$loc:general_Cycles_Combobox", 'value.value')   没有这个字段
    kV_list = scan.get_attr("$loc:DM_kV_Combobox", 'selection')
    kV = kV_list[0]
    mAs = scan.get_attr("$loc:DM_mAs_Combobox", 'value.value')

    scan.set_variable('length', length)
    scan.set_variable('collimation', collimation)
    # scan.set_variable('cycles', cycles)     没有这个字段
    scan.set_variable('kV', kV)
    scan.set_variable('mAs', mAs)

    scan.click('$loc:DoseRightIndexDM_option')
    scan.click('$loc:GO_button')
    # if scan.wait_ocr_text_present('Table is in maximum outward position',region='$loc:left_screen_window',timeout=5):
    #     scan.click('$loc:OK_button')
    sleep(3)

    # simulator_ctrl = get_page(context, SimulatorControllerPage)
    simulator.bring_to_front('$loc:EarthPlatformSimulator_window', activate=True)
    sleep(2)
    simulator.click('$loc:Scan_button', hold_duration=0.15)

    sleep(1)
    simulator.minimize_window('$loc:EarthPlatformSimulator_window')

    scan.wait_enabled('$loc:GO_button')

    scan.click('$loc:GO_button')
    sleep(3)
    simulator.bring_to_front('$loc:EarthPlatformSimulator_window', activate=True)
    sleep(2)
    simulator.click('$loc:Scan_button', hold_duration=0.15)
    sleep(1)
    simulator.minimize_window('$loc:EarthPlatformSimulator_window')
    scan.wait_enabled('$loc:Repeat_button', timeout=35)

    scan.click('$loc:Repeat_button')
    sleep(3)
    simulator.bring_to_front('$loc:EarthPlatformSimulator_window', activate=True)
    sleep(2)
    simulator.click('$loc:Scan_button', hold_duration=0.15)
    sleep(1)
    simulator.minimize_window('$loc:EarthPlatformSimulator_window')

    scan.wait_enabled('$loc:Repeat_button', timeout=35)

    scan.click('$loc:protocol3_option')
    scan.click('$loc:ShowAll_button')

    length2 = scan.get_attr("$loc:geometry_Length_Edit", 'value.value')
    collimation_list2 = scan.get_attr("$loc:general_Collimation_Combobox", 'selection')
    collimation2 = collimation_list2[0]
    # cycles2 = scan.get_attr("$loc:general_Cycles_Combobox", 'value.value')   没有这个字段
    kV_list2 = scan.get_attr("$loc:DM_kV_Combobox", 'selection')
    kV2 = kV_list2[0]
    mAs2 = scan.get_attr("$loc:DM_mAs_Combobox", 'value.value')

    scan.set_variable('length2', length2)
    scan.set_variable('collimation2', collimation2)
    # scan.set_variable('cycles2', cycles2)   没有这个字段
    scan.set_variable('kV2', kV2)
    scan.set_variable('mAs2', mAs2)

@then("the following parameters should remain the same between the two scans:")
def step_assert_scan_parameters_unchanged(context):
    """Parameter names are supplied through context.table rows."""
    # row = context.table.rows[0]
    # parameter1 = row["Parameter1"]  # "Length"
    # parameter2 = row["Parameter2"]  # "Collimation"
    # parameter3 = row["Parameter3"]  # "Cycles"
    # parameter4 = row["Parameter4"]  # "kV"
    # parameter5 = row["Parameter5"]  # "mAs"

    scan = get_page(context, LeftScreenPage).scan
    if scan.get_variable('length') != scan.get_variable('length2'):
        raise AssertionError("Length mismatch")
    if scan.get_variable('collimation') != scan.get_variable('collimation2'):
        raise AssertionError("collimation mismatch")
    # if scan.get_variable('cycles') != scan.get_variable('cycles2'):     没有这个字段
    #     raise AssertionError("cycles mismatch")     没有这个字段
    if scan.get_variable('kV') != scan.get_variable('kV2'):
        raise AssertionError("kV mismatch")
    if scan.get_variable('mAs') != scan.get_variable('mAs2'):
        print(scan.get_variable('mAs'))
        print(scan.get_variable('mAs2'))
        raise AssertionError("mAs mismatch")

@then(
    "the patient should contain two helical series: one original helical series "
    "and one generated by the repeat scan."
)
def step_assert_original_and_repeat_helical_series(context):
    series201 = {'root': 'right_screen_window', 'by': 'xpath','value': "//DataGrid[@auto_id='DataGrid_1']//Text[@name='201']"}
    series301 = {'root': 'right_screen_window', 'by': 'xpath','value': "//DataGrid[@auto_id='DataGrid_1']//Text[@name='301']"}

    completed = get_page(context, RightScreenPage).completed
    completed.input_text('$loc:PatientID_edit',completed.get_variable('patient_id'))
    completed.click('$loc:completed_first_dataItem')

    completed.assert_exists(series201)
    completed.assert_exists(series301)