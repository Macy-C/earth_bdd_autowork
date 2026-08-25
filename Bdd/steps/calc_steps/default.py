from behave import *
from Bdd.page_obj.calc.page import CalcPage
from autowork_core.page import get_page


# Scenario Outline: 计算相减 ----------------------
@when('计算 {num1} 减去 {num2} 的结果')
def calc(context,num1, num2):
    calc = get_page(context,CalcPage)
    if num1 == '1':
        calc.click('$loc:num1_button')
    elif num1 == '2':
        calc.click('$num2_button')

    calc.click('$loc:minus_button')

    if num2 == '1':
        calc.click('$loc:num1_button')
    elif num2 == '3':
        calc.click('$num3_button')

    calc.click('$loc:equal_button')

@then('结果应该是 "{res}"')
def calc(context,res):
    calc = get_page(context,CalcPage)
    calc.assert_attr_contains('$loc:calc_result','name',res)


# Scenario: 计算相乘 --------------------------
@step('计算表格内相乘的结果')
def calc(context):
    pass

@step('结果应该等于表格内的预期结果:')
def calc(context):
    calc = get_page(context,CalcPage)
    rows = [row.as_dict() for row in context.table]
    for item in rows:
        if item['num1'] == '1':
            calc.click('$loc:num1_button')
        elif item['num1'] == '2':
            calc.click('$loc:num2_button')

        calc.click('$loc:multiply_button')

        if item['num2'] == '1':
            calc.click('$loc:num1_button')
        elif item['num2'] == '3':
            calc.click('$loc:num3_button')

        calc.click('$loc:equal_button')

        calc.assert_attr_contains('$loc:calc_result', 'name', item['res'])

    """
        row = context.table
        print(row)
        #.<Table: 3x2>
        for item in row:
            print(item.cells)
            # < Row['2', '1', '1'] >
            # < Row['1', '3', '-2'] >
            print(item.cells)
            # ['2', '1', '1']
            # ['1', '3', '-2']
        rows = [row.as_dict() for row in context.table]
        print(rows)
        # [OrderedDict([('num1', '2'), ('num2', '1'), ('res', '1')]), OrderedDict([('num1', '1'), ('num2', '3'), ('res', '-2')])]
        for item in rows:
            print(item)
            # OrderedDict([('num1', '2'), ('num2', '1'), ('res', '1')])
            # OrderedDict([('num1', '1'), ('num2', '3'), ('res', '-2')])
    """