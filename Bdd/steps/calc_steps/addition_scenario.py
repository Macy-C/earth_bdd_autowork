from behave import *
from Bdd.page_obj.calc.page import CalcPage
from autowork_core.page import get_page


@when('计算 1 加 3 的结果')
def calc(context):
    calc = get_page(context,CalcPage)
    calc.click('$loc:num1_button')
    calc.click('$loc:plus_button')
    calc.click('$loc:num3_button')
    calc.click('$loc:equal_button')

@then('结果应该是 4')
def calc(context):
    calc = get_page(context,CalcPage)
    calc.assert_attr_contains('$loc:calc_result','name','4')
