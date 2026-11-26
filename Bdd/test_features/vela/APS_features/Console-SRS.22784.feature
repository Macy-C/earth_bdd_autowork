# SRS ID: CT-NM.SRS.Modes.2212
# SSRS ID: Console-SRS.22784
# SSRS Description:
#    When the automatic phase determination option is used,
#     the console shall indicate the usage of the option
#     in the generated image data and in user visible image information.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-07-27
# Updated Date: 2026-07-29
# Version: V1

@step:APS_steps/Console-SRS.22784
Feature: Console-SRS.22784
    As a CT technologist
    I want reconstructed images to identify the automatic cardiac phase determination option that was used
    So that I can trace how each image was reconstructed from both stored and visible image information

    当使用自动期相判定选项时，控制台应在生成的图像数据和用户可见的图像信息中标示所使用的选项。

    # 作为 CT 技师，我希望重建图像能够标识所使用的自动心动期相判定选项，
    # 从而可以通过已存储和用户可见的图像信息追溯每幅图像的重建方式。

    Rule: Images reconstructed with automatic cardiac phase determination identify the selected option in generated image data and user-visible image information
    # 使用自动心动期相判定重建的图像，应在生成的图像数据和用户可见的图像信息中标识所选选项。

    @gantry
    Scenario Outline: Reconstructed images identify <reconstruction_option> used for a <acquisition_type> acquisition
        Given a completed "<acquisition_type>" cardiac acquisition is available for reconstruction
        When the user reconstructs the acquisition using automatic cardiac phase determination option "<reconstruction_option>"
        Then the generated image data indicates that automatic cardiac phase determination option "<reconstruction_option>" was used
        And the image paramenters fields accurately identify "<reconstruction_option>" as the automatic cardiac phase determination option used
    Examples:
            | acquisition_type | reconstruction_option   |
            | Helical          | Precise Phase           |
            | Helical          | Precise Phase Systolic  |
            | Helical          | Precise Phase Diastolic |
            | Step & Shoot     | Precise Phase           |

    # ------------------------------------------------------
    # Scenario Outline: 重建图像标识用于 <采集类型> 采集的 <重建选项>
    #     Given 已有完成的 "<采集类型>" 心脏采集可用于重建
    #     When 用户使用自动心动期相判定选项 "<重建选项>" 重建该采集
    #     Then 生成的图像数据标识已使用自动心动期相判定选项 "<重建选项>"
    #     And  图像参数字段准确地将 "<重建选项>" 识别为用于自动心脏相位确定的选项
    #
    # Examples:
    #         | 采集类型       | 重建选项                   |
    #         | Helical        | Precise Phase             |
    #         | Helical        | Precise Phase Systolic    |
    #         | Helical        | Precise Phase Diastolic   |
    #         | Step & Shoot   | Precise Phase             |
    # ------------------------------------------------------