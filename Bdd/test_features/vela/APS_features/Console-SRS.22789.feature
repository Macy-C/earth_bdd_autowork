# SRS ID: CT-NM.SRS.Modes.2209
# SSRS ID: Console-SRS.22789
# SSRS Description:
#       The automatic cardiac phase determination reconstruction option shall
#       not be usable when the option 'full coverage, Longer Cycle Time' is ON.
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-07-27
# Updated Date: 2026-07-29
# Version: V1

@step:APS_steps/Console-SRS.22789
Feature: Console-SRS.22789
    As a CT technologist
    I want automatic cardiac phase determination reconstruction to be unavailable when Full Coverage, Longer Cycle Time is enabled
    So that incompatible reconstruction options cannot be used

    当 "Full Coverage, Longer Cycle Time" 开启时，自动心动期相判定重建选项不可用。

    # 作为 CT 技师，我希望启用 Full Coverage, Longer Cycle Time 时无法使用自动心动期相判定重建，
    # 从而避免使用不兼容的重建选项。

    Rule: Automatic cardiac phase determination reconstruction cannot be used with Full Coverage, Longer Cycle Time
    # 自动心动期相判定重建不能与 Full Coverage, Longer Cycle Time 同时使用。
    @gantry
    Scenario: Precise Phase is unavailable when Full Coverage, Longer Cycle Time is selected for Step & Shoot
        Given a patient is registered on the Console
        And the "Step & Shoot" protocol is selected
        When the user selects "Full Coverage, Longer Cycle Time"
        Then the "Precise Phase" automatic cardiac phase determination reconstruction option is unavailable
        When the user opens offline reconstruction for the Step & Shoot acquisition
        Then the "Precise Phase" automatic cardiac phase determination reconstruction option is unavailable in offline reconstruction

    # ------------------------------------------------------
    # Scenario: Step & Shoot 选择 Full Coverage, Longer Cycle Time 时 Precise Phase 不可用
    #     Given 已在控制台注册患者
    #     And 已选择 "Step & Shoot" 协议
    #     When 用户选择 "Full Coverage, Longer Cycle Time"
    #     Then "Precise Phase" 自动心动期相判定重建选项不可用
    #     When 用户打开该 Step & Shoot 采集的离线重建
    #     Then 离线重建中的 "Precise Phase" 自动心动期相判定重建选项不可用
    # ------------------------------------------------------
