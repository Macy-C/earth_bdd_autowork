# SRS ID: CT-NM.SRS.Modes.2214  CT-NM.SRS.Modes.2215
# SSRS ID: Console-SRS.22774
# SSRS Description:
# For absolute-delay-based gated acquisition in Step & Shoot scan mode,
# the Console shall request the Gantry to perform the scan with the configured absolute delay and phase tolerance.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-06-10
# Updated Date: 2026-08-03
# Version: V2
#
# log example:
#      2026.05.28 09:08:36:968617        Event        CardiacPulmoDevice
#      CardiacPulmoDevice : CRTC: Cardiac device sent Axial params to CRTC #skips: 6,
#      relative: phase: 0.78, tolerance: 0, PhaseMode: 0        CardiacServer
#      absolue:  phase: 350, tolerance: 60, PhaseMode: 1        CardiacServer
#      PhaseMode : 0代表relative, 1代表absolute

@gantry @step:Absolute_delay_steps/Console-SRS.22774
Feature: Console-SRS.22774
  The Console shall request the Gantry scan with the configured phase and phase tolerance in absolute cardiac phase mode
  so that absolute-delay-based gated acquisition is performed with the intended timing parameters.

  Rule: For absolute-delay-based gated acquisition in Step & Shoot scan mode, the Console shall request the Gantry to perform the scan with the configured phase and phase tolerance.

  Scenario Outline: Log Viewer shows configured phase parameters for the Gantry scan request in absolute mode
    Given a patient is registered on the Console
    And the "Step & Shoot" protocol is selected
    And the clinical sequence parameters are open on the Display Interface
    And the cardiac phase mode is set to "absolute" on the Display Interface
    And the phase is set to "<phase>" on the Display Interface
    And the phase tolerance is set to "<phase_tolerance>" on the Display Interface
    When "GO" is clicked on the Display Interface
    Then the Log Viewer shows a Console-to-Gantry scan parameter log
    And the log shows phase "<phase>"
    And the log shows phase tolerance "<phase_tolerance>"
    And the log shows PhaseMode "<PhaseMode>"

  Examples:
      | phase | phase_tolerance | PhaseMode |
      | 250   | 0               | 1         |
      | 450   | 0               | 1         |
      | 350   | 100             | 1         |
      | 300   | 50              | 1         |
      | 400   | 50              | 1         |
# ------------------------------------------------------
#  Scenario Outline: 日志查看器在绝对模式下显示发送给机架扫描请求的已配置相位参数
#    Given 已在控制台上完成患者注册
#    And 已选择 "Step & Shoot" 协议
#    And 显示界面上已打开临床序列参数
#    And 已在显示界面上将心动相位模式设置为 "绝对"
#    And 已在显示界面上将相位设置为 "<相位>"
#    And 已在显示界面上将相位容差设置为 "<相位容差>"
#    When 在显示界面上点击 "GO"
#    Then 日志查看器显示一条控制台到机架的扫描参数日志
#    And 日志中显示相位 "<相位>"
#    And 日志中显示相位容差 "<相位容差>"
#    And 日志中显示 PhaseMode "<相位模式>"
#
#  Examples:
#      | 相位 | 相位容差 | 相位模式 |
#      | 250  | 0        | 1        |
#      | 450  | 0        | 1        |
#      | 350  | 100      | 1        |
#      | 300  | 50       | 1        |
#      | 400  | 50       | 1        |
# ------------------------------------------------------