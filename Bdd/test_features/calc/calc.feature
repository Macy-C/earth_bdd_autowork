
@step:calc_steps/default
Feature: calc

  @step:calc_steps/addition_scenario
  Scenario: 计算相加
    When 计算 1 加 3 的结果
    Then 结果应该是 4

  #@single:none:2:example=1.2
  Scenario Outline: 计算相减
    When 计算 <num1> 减去 <num2> 的结果
    Then 结果应该是 "<res>"
  Examples:
    | num1 | num2 | res |
    |   2  |   1  |  1  |
    |   1  |  3   | -2  |


  Scenario: 计算相乘
  When 计算表格内相乘的结果
  Then 结果应该等于表格内的预期结果:
  | num1 | num2 | res |
  |   2  |   1  |  2  |
  |   1  |  3   |  3  |

