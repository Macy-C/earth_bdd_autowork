# SRS ID: CT-NM.SRS.ProductConfig.584
# SSRS ID: Console-SRS.22787
# SSRS Description:
#     The Console shall protect the automatic cardiac phase determination reconstruction  option functionality by a license Key.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-07-27
# Updated Date: 2026-07-29
# Version: V1

@step:APS_steps/Console-SRS.22787
Feature: Console-SRS.22787
    As a system administrator
    I want automatic cardiac phase determination reconstruction to be controlled by its applicable license
    So that only licensed systems can use the functionality

    控制台应通过许可证密钥保护自动心动期相判定重建选项功能。

    # 作为系统管理员，我希望自动心动期相判定重建功能由适用的许可证控制，
    # 从而只有已获得许可的系统才能使用该功能。

    Rule: Preferences indicates whether a permanent Precise Phase license is supported
    # Preferences 显示系统是否支持永久 Precise Phase 许可证。

    @license_with_absolute_delay
    Scenario: Preferences shows Permanent when a supported license key is used
        Given a supported Precise Phase license key is used
        When the user opens Preferences
        Then the License Type field displays "Permanent"

    # ------------------------------------------------------
    # Scenario: 使用受支持的许可证密钥时，Preferences 显示 Permanent
    #     Given 已使用受支持的 Precise Phase 许可证密钥
    #     When 用户打开 Preferences
    #     Then License Type 字段显示 "Permanent"
    # ------------------------------------------------------

    @license_without_absolute_delay
    Scenario: Preferences shows no license type when a supported license key is not used
        Given a supported Precise Phase license key is not used
        When the user opens Preferences
        Then the License Type field is empty

    # ------------------------------------------------------
    # Scenario: 未使用受支持的许可证密钥时，Preferences 不显示许可证类型
    #     Given 未使用受支持的 Precise Phase 许可证密钥
    #     When 用户打开 Preferences
    #     Then License Type 字段为空
    # ------------------------------------------------------

    Rule: A supported Precise Phase license enables scan and reconstruction workflows
    # 受支持的 Precise Phase 许可证启用扫描和重建工作流。

    @license_with_absolute_delay @gantry
    Scenario: Scan is supported when a supported Precise Phase license key is used
        Given a supported Precise Phase license key is used
        Given a supported cardiac acquisition is configured
        When the user starts the scan
        Then the Console starts the scan successfully

    # ------------------------------------------------------
    # Scenario: 使用受支持的 Precise Phase 许可证密钥时支持扫描
    #     Given 已使用受支持的 Precise Phase 许可证密钥
    #     And 已配置受支持的心脏采集
    #     When 用户开始扫描
    #     Then 控制台成功开始扫描
    # ------------------------------------------------------

    @license_with_absolute_delay @gantry
    Scenario: Reconstruction is supported when a supported Precise Phase license key is used
        Given a supported Precise Phase license key is used
        Given a completed supported cardiac acquisition is available for reconstruction
        When the user starts reconstruction using a Precise Phase option
        Then the Console starts the reconstruction successfully

    # ------------------------------------------------------
    # Scenario: 使用受支持的 Precise Phase 许可证密钥时支持重建
    #     Given 已使用受支持的 Precise Phase 许可证密钥
    #     And 已有完成的受支持心脏采集可用于重建
    #     When 用户使用 Precise Phase 选项开始重建
    #     Then 控制台成功开始重建
    # ------------------------------------------------------
