# SRS ID: CT-NM.SRS.ProductConfig.585
# SSRS ID: Console-SRS.22782
# SSRS Description:
#      The Console shall make the Absolute Delay parameter
#        configurable only for the following scan types: Cardiac and Step & Shoot scans.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-07-28
# Updated Date: 2026-08-03
# Version: V1

@step:Absolute_delay_steps/Console-SRS.22782
Feature: Console-SRS.22782
      The Console shall make the Absolute Delay parameter
        configurable only for the following scan types: Cardiac and Step & Shoot scans.
      控制台仅应针对以下扫描类型允许配置 Absolute Delay 参数：心脏和 Step & Shoot 扫描。

  @gantry
  Scenario: Absolute Delay is configurable for the Step & Shoot scan type
    Given the Cardiac Step & Shoot configuration page is opened from Exam Card Manager
    And a clinical sequence item is selected
    When Scan Type is set to "Step&Shoot"
    Then the Absolute Delay option should be enabled
# ------------------------------------------------------
#  Scenario: Step & Shoot 扫描类型允许配置 Absolute Delay 参数
#    Given 已从 Exam Card Manager 页面进入 Cardiac Step & Shoot 配置页
#    And 已选择临床序列项
#    When Scan Type 设置为 "Step & Shoot"
#    Then Absolute Delay 选项应可用
# ------------------------------------------------------

  @skip @covered_by_srs.22781
  Scenario: Absolute Delay is configurable during reconstruction for the Cardiac scan type
    Given a patient is registered with the Coronary CTA protocol
    And the scan and online reconstruction are performed
    When offline reconstruction is performed based on the current results
    Then the absolute-delay-based cardiac reconstruction option can be selected
# ------------------------------------------------------
#  Scenario: Cardiac 扫描类型在重建时允许配置 Absolute Delay 参数
#    Given 已使用 Coronary CTA 协议注册患者
#    And 已执行扫描和在线重建
#    When 基于当前结果执行离线重建
#    Then 基于绝对延迟的心脏重建选项可选择
# ------------------------------------------------------
# 注：第二个场景已由 SRS.22781 覆盖，因此跳过执行。

