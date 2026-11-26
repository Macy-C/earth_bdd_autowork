# SRS ID: CT-NM.SRS.ProductConfig.585
# SSRS ID: Console-SRS.22779
# SSRS Description:
#      The Console shall protect the Absolute Delay option functionality by a license Key.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-07-28
# Updated Date: 2026-08-03
# Version: V1

@step:Absolute_delay_steps/Console-SRS.22779
Feature: Console-SRS.22779
      The Console shall protect the Absolute Delay option functionality by a license Key.
      控制台应通过许可证密钥保护 Absolute Delay 选项功能。

  @license_without_absolute_delay
  Scenario: Absolute Delay license value is empty when the license does not support Absolute Delay
    Given a license that does not support Absolute Delay is configured on the Console
    When the licensing interface is opened in Preferences
    Then the value of the Absolute Delay item should be empty
# ------------------------------------------------------
#  Scenario: 当许可证不支持 Absolute Delay 时，Absolute Delay 许可证项值为空
#    Given 控制台已配置不支持 Absolute Delay 的许可证
#    When 已在首选项中打开许可证界面
#    Then Absolute Delay 项的值应为空
# ------------------------------------------------------

  @license_with_absolute_delay
  Scenario: Absolute Delay license value is Permanent when the license supports Absolute Delay
    Given a license that supports Absolute Delay is configured on the Console
    When the licensing interface is opened in Preferences
    Then the value of the Absolute Delay item should be "Permanent"
# ------------------------------------------------------
#  Scenario: 当许可证支持 Absolute Delay 时，Absolute Delay 许可证项值为 "Permanent"
#    Given 控制台已配置支持 Absolute Delay 的许可证
#    When 已在首选项中打开许可证界面
#    Then Absolute Delay 项的值应为 "Permanent"
# ------------------------------------------------------
