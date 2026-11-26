# SRS ID: CT-NM.SRS.Modes.2217
# SSRS ID: Console-SRS.22777
# SSRS Description:
# The Console shall provide an absolute-delay-based cardiac reconstruction option that
# allows the user to specify reconstruction phase in milliseconds referenced to the cardiac cycle reference.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-06-10
# Updated Date: 2026-08-03
# Version: V2

@step:Absolute_delay_steps/Console-SRS.22777
Feature: Console-SRS.22777
  The Console shall provide an absolute-delay-based cardiac reconstruction option
  so that reconstruction phase can be specified in milliseconds referenced to the cardiac cycle reference.

  Rule: The Console shall provide an absolute-delay-based cardiac reconstruction option.
  Rule: When the absolute-delay-based cardiac reconstruction option is selected, the Console shall allow reconstruction phase to be specified in milliseconds referenced to the cardiac cycle reference.
  Rule: The Display Interface shall show reconstruction phase values and units according to the selected cardiac phase mode.

  Scenario Outline: Reconstruction phase presentation follows the selected cardiac phase mode
    Given a patient is registered on the Console
    And the "Step & Shoot" protocol is selected
    And the clinical sequence parameters are open on the Display Interface
    And the cardiac phase mode is set to "<cardiac_phase_mode>" on the Display Interface
    And the reconstruction phase is set to "<reconstruction_phase>" on the Display Interface
    And the phase tolerance is set to "<phase_tolerance>" on the Display Interface
    When the reconstruction sequence is selected
    Then the reconstruction phase range shown on the Display Interface is "<reconstruction_phase_range>"
    And the reconstruction phase unit shown on the Display Interface is "<reconstruction_phase_unit>"
    And the reconstructed series name includes "<series_name>"

  Examples:
      | cardiac_phase_mode | reconstruction_phase | phase_tolerance | reconstruction_phase_range | reconstruction_phase_unit | series_name                        |
      | relative           | 78 %                 | 0 %             | 78                         | %                         | 78%,None,Relative,0.8x0.4          |
      | relative           | 78 %                 | 5 %             | 73 to 83                   | %                         | 78%,None,Relative,0.8x0.4          |
      | absolute           | 350 ms               | 0 ms            | 350                        | ms                        | 350ms,None,Absolute,0.8x0.4        |
      | absolute           | 350 ms               | 100 ms          | 250 to 450                 | ms                        | 350ms,None,Absolute,0.8x0.4        |
# ------------------------------------------------------
#  Scenario Outline: 重建相位的显示方式随所选心动相位模式变化
#    Given 已在控制台上完成患者注册
#    And 已选择 "Step & Shoot" 协议
#    And 显示界面上已打开临床序列参数
#    And 已在显示界面上将心动相位模式设置为 "<心动相位模式>"
#    And 已在显示界面上将重建相位设置为 "<重建相位>"
#    And 已在显示界面上将相位容差设置为 "<相位容差>"
#    When 选择重建序列
#    Then 显示界面上显示的重建相位范围为 "<重建相位范围>"
#    And 显示界面上显示的重建相位单位为 "<重建相位单位>"
#    And 重建后的序列名称包含 "<序列名称>"
#
#  Examples:
#      | 心动相位模式 | 重建相位 | 相位容差 | 重建相位范围 | 重建相位单位 | 序列名称                         |
#      | relative     | 78 %     | 0 %      | 78           | %            | 78%,None,Relative,0.8x0.4       |
#      | relative     | 78 %     | 5 %      | 73 to 83     | %            | 78%,None,Relative,0.8x0.4       |
#      | absolute     | 350 ms   | 0 ms     | 350          | ms           | 350ms,None,Absolute,0.8x0.4     |
#      | absolute     | 350 ms   | 100 ms   | 250 to 450   | ms           | 350ms,None,Absolute,0.8x0.4     |
# ------------------------------------------------------