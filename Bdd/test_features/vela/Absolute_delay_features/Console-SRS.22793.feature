# SRS ID: CT-NM.SRS.Modes.2214
# SSRS ID: Console-SRS.22793
# SSRS Description:
# For Step & Shoot scan mode, the Host shall allow user selection of absolute-delay-based gated acquisition
# and shall allow configuration of absolute delay and phase tolerance.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-06-04
# Updated Date: 2026-08-03
# Version: V1

@step:Absolute_delay_steps/Console-SRS.22793
Feature: Console-SRS.22793
  The Host shall allow selection of absolute-delay-based gated acquisition and configuration of absolute delay and phase tolerance
  relative to the cardiac cycle.

  Rule: For Step & Shoot scan mode, the Host shall allow selection of absolute-delay-based gated acquisition.
  Rule: When absolute-delay-based gated acquisition is selected, the Host shall allow configuration of absolute delay and phase tolerance.
  Rule: The Display Interface shall show phase and phase tolerance units according to the selected cardiac phase mode.

  Background:
    Given a patient is registered on the Host
    And the "Step & Shoot" protocol is selected
    And the clinical sequence parameters are open on the Display Interface

  Scenario: Absolute-delay-based gated acquisition can be configured in Step & Shoot mode
    When the cardiac phase mode configuration is displayed on the Display Interface
    Then the available cardiac phase mode options are "Relative" and "Absolute"
    And "Relative" is selected as the default cardiac phase mode
# ------------------------------------------------------
#  Scenario: 可在 Step & Shoot 模式下配置基于绝对延迟的门控采集
#    When 显示界面显示心动相位模式配置
#    Then 可用的心动相位模式选项为 "Relative" 和 "Absolute"
#    And 默认选中的心动相位模式为 "Relative"
# ------------------------------------------------------

  Scenario Outline: Phase and phase tolerance units follow the selected cardiac phase mode
    When the cardiac phase mode is set to "<cardiac_phase_mode>" on the Display Interface
    Then the phase unit shown on the Display Interface is "<phase_unit>"
    And the phase tolerance unit shown on the Display Interface is "<phase_tolerance_unit>"

  Examples:
      | cardiac_phase_mode | phase_unit | phase_tolerance_unit |
      | relative           | %          | %                    |
      | absolute           | ms         | ms                   |
# ------------------------------------------------------
#  Scenario Outline: 相位和相位容差单位随所选心动相位模式变化
#    When 在显示界面上将心动相位模式设置为 "<心动相位模式>"
#    Then 显示界面上显示的相位单位为 "<相位单位>"
#    And 显示界面上显示的相位容差单位为 "<相位容差单位>"
#
#  Examples:
#      | 心动相位模式 | 相位单位 | 相位容差单位 |
#      | relative     | %        | %            |
#      | absolute     | ms       | ms           |
# ------------------------------------------------------
