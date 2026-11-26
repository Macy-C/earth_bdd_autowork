# SRS ID: CT-NM.SRS.Modes.2209
# SSRS ID: Console-SRS.22790
# SSRS Description:
#       For Step & Shoot scan mode, the automatic cardiac phase determination reconstruction
#       option shall be available when Phase Tolerance is greater than or equal to 2% or 30ms.
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-07-27
# Updated Date: 2026-07-30
# Version: V1

@step:APS_steps/Console-SRS.22790 @SKIP @notfinsh
Feature: Console-SRS.22790
    As a CT technologist
    I want Precise Phase reconstruction to be available when phase tolerance meets its supported threshold
    So that automatic cardiac phase determination can be used for eligible Step & Shoot acquisitions

    对于 Step & Shoot 扫描模式，当 Phase Tolerance 大于等于 2% 或 30 ms 时，Precise Phase 重建选项应可用。

    # 作为 CT 技师，我希望 Phase Tolerance 达到受支持阈值时 Precise Phase 重建可用，
    # 从而能够对符合条件的 Step & Shoot 采集使用自动心动期相判定。

    Rule: Precise Phase is available for Step & Shoot when Relative phase tolerance is at least 2% or Absolute phase tolerance is at least 30 ms
    # Step & Shoot 的 Relative Phase Tolerance 至少为 2%，或 Absolute Phase Tolerance 至少为 30 ms 时，Precise Phase 可用。
    Scenario Outline: Precise Phase is available for <cardiac_phase_mode> phase tolerance <phase_tolerance>
        Given a patient is registered on the Console
        And the "Step & Shoot" protocol is selected
        And the cardiac phase mode is set to "<cardiac_phase_mode>"
        When the phase tolerance is set to "<phase_tolerance>"
        Then the "Precise Phase" automatic cardiac phase determination reconstruction option is available

    Examples:
            | cardiac_phase_mode | phase_tolerance |
            | Relative           | 2%              |
            | Relative           | 5%              |
            | Absolute           | 30 ms           |
            | Absolute           | 35 ms           |

          # ------------------------------------------------------
          # Scenario Outline: <心动相位模式> 的 Phase Tolerance 为 <相位容差> 时 Precise Phase 可用
          #     Given 已在控制台注册患者
          #     And 已选择 "Step & Shoot" 协议
          #     And 心动相位模式已设置为 "<心动相位模式>"
          #     When Phase Tolerance 已设置为 "<相位容差>"
          #     Then "Precise Phase" 自动心动期相判定重建选项可用
          #
          # Examples:
          #         | 心动相位模式   | 相位容差   |
          #         | Relative       | 2%         |
          #         | Relative       | 5%         |
          #         | Absolute       | 30 ms      |
          #         | Absolute       | 35 ms      |
          # ------------------------------------------------------

    Rule: When the relative phase tolerance is below 2% or the absolute phase tolerance is below 30 milliseconds, the Precise Phase function cannot be used
          # Relative Phase Tolerance 低于 2% 或 Absolute Phase Tolerance 低于 30 ms 时，Precise Phase 不可用。
    Scenario Outline: Phase Tolerance 小于 2% 或 30 ms
      Given a patient is registered on the Console
      And the "Step & Shoot" protocol is selected
      And the cardiac phase mode is set to "<cardiac_phase_mode>"
      When the phase tolerance is set to "<phase_tolerance>"
      Then the "precise phase" automatic heart phase determination reconstruction option is unavailable

    Examples:
            |cardiac_phase_mode|phase_tolerance |unit|
            |Relative          |1               |%   |
            |Absolute          |20              |ms  |

      # ------------------------------------------------------
      # Scenario Outline: Phase Tolerance 小于 2% 或 30 ms 时 Precise Phase 不可用
      #     Given 已在控制台注册患者
      #     And 已选择 "Step & Shoot" 协议
      #     And 心动相位模式已设置为 "<心动相位模式>"
      #     When Phase Tolerance 已设置为 "<相位容差>"
      #     Then "Precise Phase" 自动心动期相判定重建选项不可用
      #
      # Examples:
      #         | 心动相位模式   | 相位容差 | 单位 |
      #         | Relative       | 1        | %    |
      #         | Absolute       | 20       | ms   |
      # ------------------------------------------------------
