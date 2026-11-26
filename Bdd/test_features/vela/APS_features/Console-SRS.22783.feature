# SRS ID: CT-NM.SRS.Modes.2209 CT-NM.SRS.Modes.2210 CT-NM.SRS.Modes.2211
# SSRS ID: Console-SRS.22783
# SSRS Description:
#     The Console shall provide the available automatic cardiac phase determination reconstruction options by acquisition type as follows:
#     - For helical cardiac acquisitions, the console shall provide Precise Phase, Precise Phase Systolic, and Precise Phase Diastolic.
#     - For step & shoot cardiac acquisitions, the console shall provide Precise Phase.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-07-27
# Updated Date: 2026-07-29
# Version: V1

@step:APS_steps/Console-SRS.22783
Feature: Console-SRS.22783
    As a CT technologist
    I want the available automatic cardiac phase determination reconstruction options to match the acquisition type
    So that I can select only reconstruction options supported by the acquisition

    控制台应按采集类型提供可用的自动心动期相判定重建选项。
    # 作为 CT 技师，我希望可用的自动心动期相判定重建选项与采集类型相匹配，
    # 从而只能选择当前采集类型支持的重建选项。

    Rule: Helical cardiac acquisitions provide Precise Phase, Precise Phase Systolic, and Precise Phase Diastolic
    #螺旋心脏采集提供 Precise Phase、Precise Phase Systolic 和 Precise Phase Diastolic。

    Scenario: Helical cardiac acquisition provides all supported automatic phase options
        Given a cardiac examination is configured with a helical cardiac acquisition
        When the user opens the automatic cardiac phase determination reconstruction options
        Then the Console provides the following automatic cardiac phase determination reconstruction options:
            | option                  |
            | Precise Phase           |
            | Precise Phase Systolic  |
            | Precise Phase Diastolic |

    # ------------------------------------------------------
    # Scenario: 螺旋心脏采集提供全部受支持的自动期相选项
    #     Given 已配置包含螺旋心脏采集的心脏检查
    #     When 用户打开自动心动期相判定重建选项
    #     Then 控制台提供以下自动心动期相判定重建选项：
    #         | 选项                     |
    #         | Precise Phase            |
    #         | Precise Phase Systolic   |
    #         | Precise Phase Diastolic  |
    # ------------------------------------------------------
    @gantry
    Scenario: Reconstruct a helical cardiac acquisition using reconstruction_option
        Given a completed helical cardiac acquisition is available for reconstruction
        When the user selects reconstruction_option and starts reconstruction
        Then the reconstructed images are available for review
            | option                   |
            | Precise Phase            |
            | Precise Phase Systolic   |
            | Precise Phase Diastolic  |

    # ------------------------------------------------------
    # Scenario: 使用 <重建选项> 重建螺旋心脏采集
    #     Given 已有完成的螺旋心脏采集可用于重建
    #     When 用户选择重建选项并开始重建
    #     Then 重建图像可供查看
    #         | 重建选项                   |
    #         | Precise Phase             |
    #         | Precise Phase Systolic    |
    #         | Precise Phase Diastolic   |
    # ------------------------------------------------------

    Rule: Step & Shoot cardiac acquisitions provide Precise Phase
    # Step & Shoot 心脏采集提供 Precise Phase。

    Scenario: Step & Shoot cardiac acquisition provides only the supported automatic phase option
        Given a cardiac examination is configured with a Step & Shoot cardiac acquisition
        When the user opens the automatic cardiac phase determination reconstruction options
        Then the Console provides only the following automatic cardiac phase determination reconstruction option:
            | option        |
            | Precise Phase |

    # ------------------------------------------------------
    # Scenario: Step & Shoot 心脏采集仅提供受支持的自动期相选项
    #     Given 已配置包含 Step & Shoot 心脏采集的心脏检查
    #     When 用户打开自动心动期相判定重建选项
    #     Then 控制台仅提供以下自动心动期相判定重建选项：
    #         | 选项          |
    #         | Precise Phase |
    # ------------------------------------------------------
    @gantry
    Scenario: Reconstruct a Step & Shoot cardiac acquisition using Precise Phase
        Given a completed Step & Shoot cardiac acquisition is available for reconstruction
        When the user selects "Precise Phase" and starts reconstruction
        Then the reconstructed images are available for review
            | option        |
            | Precise Phase |
    # ------------------------------------------------------
    # Scenario: 使用 Precise Phase 重建 Step & Shoot 心脏采集
    #     Given 已有完成的 Step & Shoot 心脏采集数据可用于重建
    #     When 用户选择 "Precise Phase" 并开始重建
    #     Then 重建图像可供查看
    # ------------------------------------------------------