# SRS ID: CT-NM.SRS.ProductConfig.585
# SSRS ID: Console-SRS.22781
# SSRS Description:
#      The Console shall provide selection of absolute delay based
#        cardiac reconstruction only when the applicable license option is enabled.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-07-28
# Updated Date: 2026-08-03
# Version: V1

@step:Absolute_delay_steps/Console-SRS.22781 @gantry
Feature: Console-SRS.22787
      The Console shall provide selection of absolute delay based
        cardiac reconstruction only when the applicable license option is enabled.
      仅当相应许可证选项已启用时，控制台才应提供基于绝对延迟的心脏重建选择。

  @license_with_absolute_delay
  Scenario: Absolute-delay-based cardiac reconstruction can be selected for Coronary CTA when the license supports Absolute Delay
    Given a license that supports Absolute Delay is configured on the Console
    And a patient is registered with the Coronary CTA protocol
    And the scan and online reconstruction are performed
    When offline reconstruction is performed based on the current results
    Then the absolute-delay-based cardiac reconstruction option can be selected
# ------------------------------------------------------
#    Scenario: 当许可证支持 Absolute Delay 时，可为 Coronary CTA 选择基于绝对延迟的心脏重建
#      Given 控制台已配置支持 Absolute Delay 的许可证
#      And 已使用 Coronary CTA 协议注册患者
#      And 已执行扫描和在线重建
#      When 基于当前结果执行离线重建
#      Then 基于绝对延迟的心脏重建选项可选择
# ------------------------------------------------------
