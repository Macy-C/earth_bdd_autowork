# SRS ID: CT-NM.SRS.Modes.2214  CT-NM.SRS.Modes.2216
# SSRS ID: Console-SRS.22775
# SSRS Description:
#    For absolute delay based gated acquisition in Step & Shoot scan mode,
#      the Console shall limit the configurable absolute delay time to the range of 250 ms to 450 ms with 10 ms resolution.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-06-04
# Updated Date: 2026-08-03
# Version: V1

@step:Absolute_delay_steps/Console-SRS.22775
Feature: Console-SRS.22775
    For absolute delay based gated acquisition in Step & Shoot scan mode,
      the Console shall limit the configurable absolute delay time to the range of 250 ms to 450 ms with 10 ms resolution.
    对于 Step & Shoot 扫描模式下基于绝对延迟的门控采集，控制台应将可配置的绝对延迟时间限制在 250 ms 到 450 ms 范围内，分辨率为 10 ms。

  Scenario: Absolute phase input is clamped and rounded to supported values
    Given a patient is registered on the console
    And the "Step & Shoot" protocol is selected
    And the clinical sequence parameters are open on the Display Interface
    And the cardiac phase mode is set to "Absolute" on the Display Interface
    Then the corresponding supported value should be displayed for each entered absolute phase value:
      | entered_phase | displayed_phase | unit |
      | 240           | 250             | ms   |
      | 250           | 250             | ms   |
      | 450           | 450             | ms   |
      | 460           | 450             | ms   |
      | 255           | 260             | ms   |
      | 254           | 250             | ms   |
# ------------------------------------------------------
#  Scenario: 绝对相位输入应限制并取整到支持值
#    Given 已在控制台注册患者
#    And 已选择 "Step & Shoot" 协议
#    And 已在显示界面打开临床序列参数
#    And 显示界面的心动相位模式已设置为 "Absolute"
#    Then 每个输入的绝对相位值均应对应显示其支持值:
#      | 输入相位 | 显示相位 |
#      | 240 ms   | 250 ms   |
#      | 250 ms   | 250 ms   |
#      | 450 ms   | 450 ms   |
#      | 460 ms   | 450 ms   |
#      | 255 ms   | 260 ms   |
#      | 254 ms   | 250 ms   |
# ------------------------------------------------------
