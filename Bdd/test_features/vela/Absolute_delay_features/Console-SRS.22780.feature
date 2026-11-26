# SRS ID: CT-NM.SRS.ProductConfig.585
# SSRS ID: Console-SRS.22780
# SSRS Description:
#      The Console shall provide selection of absolute delay based gated acquisition
#        in Step & Shoot scan mode only when the applicable license option is enabled.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-07-28
# Updated Date: 2026-08-03
# Version: V1

@step:Absolute_delay_steps/Console-SRS.22780
Feature: Console-SRS.22780
      The Console shall provide selection of absolute delay based gated acquisition
        in Step & Shoot scan mode only when the applicable license option is enabled.
      仅当相应许可证选项已启用时，控制台才应在 Step & Shoot 扫描模式下提供基于绝对延迟的门控采集选择。

  @license_without_absolute_delay
  Scenario: Absolute-delay-based gated acquisition cannot be selected when the license does not support Absolute Delay
    Given a license that does not support Absolute Delay is configured on the Console
    And a patient is registered with the Step & Shoot protocol
    When the Step & Shoot AX Parameters interface is opened
    Then the absolute-delay-based gated acquisition option cannot be selected
# ------------------------------------------------------
#  Scenario: 当许可证不支持 Absolute Delay 时，基于绝对延迟的门控采集不可选择
#    Given 控制台已配置不支持 Absolute Delay 的许可证
#    And 已使用 Step & Shoot 协议注册患者
#    When 已打开 Step & Shoot 的 AX Parameters 界面
#    Then 基于绝对延迟的门控采集选项不可选择
# ------------------------------------------------------


  @license_with_absolute_delay
  Scenario: Absolute-delay-based gated acquisition can be selected when the license supports Absolute Delay
    Given a license that supports Absolute Delay is configured on the Console
    And a patient is registered with the Step & Shoot protocol
    When the Step & Shoot AX Parameters interface is opened
    Then the absolute-delay-based gated acquisition option can be selected
# ------------------------------------------------------
#  Scenario: 当许可证支持 Absolute Delay 时，基于绝对延迟的门控采集可选择
#    Given 控制台已配置支持 Absolute Delay 的许可证
#    And 已使用 Step & Shoot 协议注册患者
#    When 已打开 Step & Shoot 并进入 AX Parameters 界面
#    And 已打开 Step & Shoot 的 AX Parameters 界面
#    Then 基于绝对延迟的门控采集选项可选择
# ------------------------------------------------------
