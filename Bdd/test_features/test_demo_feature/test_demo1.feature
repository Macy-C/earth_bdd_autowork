# recorder-feature-id: feature-9962fd044af2
Feature: 记事本

  Scenario: 输入和删除
    Given 新建标签页
    Given 设置字体加粗
    Given 设置字体斜体
    When 输入 123456
    And 删除34
    Then 应该显示1256