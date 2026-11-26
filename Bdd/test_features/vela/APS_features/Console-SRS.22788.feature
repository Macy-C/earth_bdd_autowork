# SRS ID: CT-NM.SRS.ProductConfig.584
# SSRS ID: Console-SRS.22788
# SSRS Description:
#       The Console shall provide the automatic cardiac phase determination reconstruction
#       option only for the following scan types: Cardiac, Step & Shoot, and CTA Gated.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-07-27
# Updated Date: 2026-07-27
# Version: V1

@gantry @step:APS_steps/Console-SRS.22788
Feature: Console-SRS.22788
    As a CT technologist
    I want automatic cardiac phase determination reconstruction to be available for supported scan types
    So that I can use the option for eligible cardiac reconstructions

    # 控制台仅应为 Cardiac、Step & Shoot 和 CTA Gated 扫描类型提供自动心动期相判定重建选项。
    # 作为 CT 技师，我希望受支持的扫描类型可以使用自动心动期相判定重建，
    # 从而能够对符合条件的心脏扫描进行重建。
    # 重建操作由 Console-SRS.22784 覆盖。

    Rule: Automatic cardiac phase determination reconstruction is available for Cardiac, Step & Shoot, and CTA Gated scans
    # Cardiac、Step & Shoot 和 CTA Gated 扫描可以使用自动心动期相判定重建选项。

    Scenario: Automatic cardiac phase determination reconstruction is available for Cardiac scans
        Given patient information is prepared for a Cardiac scan
        When the user opens the reconstruction page for the Cardiac scan
        Then the automatic cardiac phase determination reconstruction option is available on the reconstruction page

    # ------------------------------------------------------
    # Scenario: Cardiac 扫描可以使用自动心动期相判定重建选项
    #     Given 已准备用于 Cardiac 扫描的患者信息
    #     When 用户打开 Cardiac 扫描的重建页面
    #     Then 重建页面上的自动心动期相判定重建选项可用
    # ------------------------------------------------------
    Scenario: Automatic cardiac phase determination reconstruction is available for Step & Shoot scans
        Given patient information is prepared for a Step & Shoot scan
        When the user opens the reconstruction page for the Step & Shoot scan
        Then the automatic cardiac phase determination reconstruction option is available on the reconstruction page

    # ------------------------------------------------------
    # Scenario: Step & Shoot 扫描可以使用自动心动期相判定重建选项
    #     Given 已准备用于 Step & Shoot 扫描的患者信息
    #     When 用户打开 Step & Shoot 扫描的重建页面
    #     Then 重建页面上的自动心动期相判定重建选项可用
    # ------------------------------------------------------

    Scenario: Automatic cardiac phase determination reconstruction is available for CTA Gated scans
        Given patient information is prepared for a CTA Gated scan
        When the user opens the reconstruction page for the CTA Gated scan
        Then the automatic cardiac phase determination reconstruction option is available on the reconstruction page

    # ------------------------------------------------------
    # Scenario: CTA Gated 扫描可以使用自动心动期相判定重建选项
    #     Given 已准备用于 CTA Gated 扫描的患者信息
    #     When 用户打开 CTA Gated 扫描的重建页面
    #     Then 重建页面上的自动心动期相判定重建选项可用
    # ------------------------------------------------------
