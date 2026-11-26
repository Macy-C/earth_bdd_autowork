# SRS ID: CT-NM.SRS.Modes.2214  CT-NM.SRS.Modes.2216
# SSRS ID: Console-SRS.22776
# SSRS Description:
#     For absolute delay based gated acquisition in Step & Shoot scan mode,
#      the Console shall limit the configurable timing buffer to the range
#      of 0 ms to 100 ms with 10 ms resolution. The selectable and valid value ranges
#      for Phase Tolerance shall auto-adjust based on the Phase value, to ensure that both
#      [Acquisition phase − Phase Tolerance] and [Acquisition phase + Phase Tolerance] fall within the valid interval of 250–450 ms.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-06-04
# Updated Date: 2026-08-03
# Version: V1

@step:Absolute_delay_steps/Console-SRS.22776
Feature: Console-SRS.22776
    For absolute delay based gated acquisition in Step & Shoot scan mode,
      the Console shall limit the configurable timing buffer to the range
      of 0 ms to 100 ms with 10 ms resolution. The selectable and valid value ranges
      for Phase Tolerance shall auto-adjust based on the Phase value, to ensure that both
      [Acquisition phase − Phase Tolerance] and [Acquisition phase + Phase Tolerance] fall within the valid interval of 250–450 ms.
    对于 Step & Shoot 扫描模式下基于绝对延迟的门控采集，控制台应将可配置的 timing buffer 限制在 0 ms 到 100 ms 范围内，
      分辨率为 10 ms。Phase Tolerance 的可选和值有效范围应根据 Phase 值自动调整，
      以确保 [Acquisition phase − Phase Tolerance] 和 [Acquisition phase + Phase Tolerance] 都落在 250–450 ms 的有效区间内。

  Rule: For absolute delay based gated acquisition in Step & Shoot scan mode,the Console shall limit the configurable timing buffer to the range of 0 ms to 100 ms with 10 ms resolution,
          The value of Phase Tolerance should follow rounding rules and be multiple of 10.
  Scenario: Phase tolerance values follow the supported range, step size, and rounding rules
    Given a patient is registered on the console
    And the "Step & Shoot" protocol is selected
    And the clinical sequence parameters are open on the Display Interface
    And the cardiac phase mode is set to "Absolute" on the Display Interface
    When the phase value is set to 350
    Then the corresponding supported value should be displayed for each entered phase tolerance value:
      | phase_tolerance_input | expected_phase_tolerance | unit |
      | 0                     | 0                        | ms   |
      | 4                     | 0                        | ms   |
      | 5                     | 10                       | ms   |
      | 101                   | 100                      | ms   |
      | 100                   | 100                      | ms   |
# ------------------------------------------------------
#  Scenario: 相位容差值遵循支持的范围、步长和取整规则
#    Given 已在控制台注册患者
#    And 已选择 "Step & Shoot" 协议
#    And 已在显示界面打开临床序列参数
#    And 显示界面的心动相位模式已设置为 "Absolute"
#    When 将相位值设置为 350
#    Then 每个输入的相位容差值均应对应显示其支持值:
#      | 输入相位容差 | 期望相位容差 |
#      | 0 ms         | 0 ms         |
#      | 4 ms         | 0 ms         |
#      | 5 ms         | 10 ms        |
#      | 101 ms       | 100 ms       |
#      | 100 ms       | 100 ms       |
# ------------------------------------------------------

    Rule: for Phase Tolerance shall auto-adjust based on the Phase value
    Scenario: Phase tolerance adjusts to the configured phase
      Given a patient is registered on the console
      And the "Step & Shoot" protocol is selected
      And the clinical sequence parameters are open on the Display Interface
      And the cardiac phase mode is set to "Absolute" on the Display Interface
      Then the corresponding phase tolerance value should be displayed for each entered phase and phase tolerance combination:
        | phase_value | phase_tolerance_input | expected_phase_tolerance |
        | 250         | 0                     | 0                        |
        | 450         | 0                     | 0                        |
        | 350         | 101                   | 100                      |
        | 300         | 51                    | 50                       |
        | 400         | 51                    | 50                       |
# ------------------------------------------------------
#  Scenario: 相位容差随已配置的相位调整
#      Given 已在控制台注册患者
#      And 已选择 "Step & Shoot" 协议
#      And 已在显示界面打开临床序列参数
#      And 显示界面的心动相位模式已设置为 "Absolute"
#      Then 每组输入的相位值和相位容差组合均应对应显示其相位容差值:
#        | 相位值 | 输入相位容差 | 期望相位容差 |
#        | 250    | 0            | 0            |
#        | 450    | 0            | 0            |
#        | 350    | 101          | 100          |
#        | 300    | 51           | 50           |
#        | 400    | 51           | 50           |
# ------------------------------------------------------

    Rule: If the phase tolerance is not zero, the range prompt information should be displayed below; if it is zero, the prompt should not be shown.
    Scenario: Prompt range reflects the configured phase and phase tolerance
      Given a patient is registered on the console
      And the "Step & Shoot" protocol is selected
      And the clinical sequence parameters are open on the Display Interface
      And the cardiac phase mode is set to "Absolute" on the Display Interface
      Then the prompt range should be independently displayed for each entered phase and phase tolerance combination:
        | phase_value | phase_tolerance_input | expected_range |
        | 250         | 0                     | 0              |
        | 450         | 0                     | 0              |
        | 350         | 100                   | 250-450        |
        | 260         | 10                    | 250-270        |
# ------------------------------------------------------
#  Scenario: 范围提示反映已配置的相位和相位容差
#      Given 已在控制台注册患者
#      And 已选择 "Step & Shoot" 协议
#      And 已在显示界面打开临床序列参数
#      And 显示界面的心动相位模式已设置为 "Absolute"
#      Then 每组输入的相位值和相位容差均应独立显示对应的范围提示:
#        | 相位值 | 输入相位容差 | 期望范围 |
#        | 250    | 0            | 0        |
#        | 450    | 0            | 0        |
#        | 350    | 100          | 250-450  |
#        | 260    | 10           | 250-270  |
# ------------------------------------------------------
