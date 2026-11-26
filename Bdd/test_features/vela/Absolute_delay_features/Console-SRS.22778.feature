# SRS ID: TBD
# SSRS ID: Console-SRS.22778
# SSRS Description:
# When the absolute-delay-based cardiac reconstruction option is used,
# the console shall indicate the usage of the option in the generated image data and in user-visible image information.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-06-11
# Updated Date: 2026-08-03
# Version: V1

@step:Absolute_delay_steps/Console-SRS.22778/default.py @gantry
Feature: Console-SRS.22778
  Use of the absolute-delay-based cardiac reconstruction option shall be indicated
  in the generated image data and in user-visible image information.
  This allows images reconstructed using the absolute-delay-based cardiac reconstruction option to be easily identified.
  应将基于绝对延迟的心脏重建选项用于生成的图像数据以及用户可见的图像信息中

  @step:Absolute_delay_steps/Console-SRS.22778/scenario1
  Scenario Outline: "Step & Shoot" final reconstruction View shows the selected Relative or Absolute indicator
    Given a patient is registered on the Host
    And a Step & Shoot cardiac scan is planned with the "<cardiac_phase_mode>" cardiac phase mode
    And the scan phase is configured as "<scan_phase_value>"
    And the scan phase tolerance is configured as "<scan_phase_tolerance_value>"
    And the Step & Shoot scan is performed

    When the final reconstruction is started for use "<recon_phase_value>"
    Then in the View, the four-corner image information should contain the Relative or Absolute indicator consistent with "<recon_phase_value>" in the Examples table
    And in Image Parameters, the Relative or Absolute indicator should be consistent with "<recon_phase_value>" in the Examples table
    When the reconstruction result is edited in Surview
    Then in the View, the Relative or Absolute indicator should be consistent with "<recon_phase_value>" in the Examples table

  Examples:
      | cardiac_phase_mode | scan_phase_value | scan_phase_tolerance_value | recon_phase_value |
      | absolute           | 350 ms           | 100 ms                     | 300ms             |
      | relative           | 78%              | 5 %                        | 80%              |
# ------------------------------------------------------
#  Scenario Outline: "Step & Shoot" 最终重建视图显示所选的 Relative 或 Absolute 标识
#    Given 已在主机上注册患者
#    And 已使用 "<心动相位模式>" 心动相位模式规划 Step & Shoot 心脏扫描
#    And 扫描相位已配置为 "<扫描相位值>"
#    And 扫描相位容差已配置为 "<扫描相位容差值>"
#    And 已执行 Step & Shoot 扫描
#
#    When 启动最终重建 for use "<recon_phase_value>"
#    Then 视图中的四角图像信息应包含与 Examples 表中 "<重建相位值>" 一致的 Relative 或 Absolute 标识
#    Then Image Parameters 中的 Relative 或 Absolute 标识应与 Examples 表中的 "<重建相位值>" 一致
#    When 在 Surview 中编辑重建结果
#    Then 视图中的 Relative 或 Absolute 标识应与 Examples 表中的 "<重建相位值>" 一致

#  Examples:
#      | 心动相位模式 | 扫描相位值 | 扫描相位容差值 | 重建相位值 |
#      | absolute     | 350 ms     | 100 ms         | 300 ms     |
#      | relative     | 78%        | 5 %            | 80 %       |
# ------------------------------------------------------


  @step:Absolute_delay_steps/Console-SRS.22778/scenario2
  Scenario: Helical protocol without Cardiac DoseRight does not show the absolute cardiac phase configuration
    Given a patient is registered on the Host
    And the "Coronary CTA" protocol is selected
    And Cardiac DoseRight is not selected

    When the reconstruction sequence parameters dialog is opened
    Then the absolute cardiac phase configuration option should not be displayed
# ------------------------------------------------------
#  Scenario: 未启用 Cardiac DoseRight 的 Helical 协议不显示绝对心动相位配置
#    Given 已在主机上注册患者
#    And 已选择 "Coronary CTA" 协议
#    And 未选择 Cardiac DoseRight
#
#    When 打开重建序列参数对话框
#    Then 不应显示绝对心动相位配置选项
# ------------------------------------------------------

  @step:Absolute_delay_steps/Console-SRS.22778/scenario3
  Scenario: Helical reconstruction Result Parameters provides Relative and Absolute options
    Given a patient is registered on the Host
    And the "Coronary CTA" protocol is selected
    And Cardiac DoseRight is not selected
    And the scan is performed
    And the final reconstruction is started

    When the reconstruction result is edited in Surview
    Then the Result Parameters should provide the "Relative" and "Absolute" cardiac phase mode options
# ------------------------------------------------------
#  Scenario: Helical 重建结果的 Result Parameters 提供 Relative 和 Absolute 选项
#    Given 已在主机上注册患者
#    And 已选择 "Coronary CTA" 协议
#    And 未选择 Cardiac DoseRight
#    And 已执行扫描
#    And 已启动最终重建
#
#    When 在 Surview 中编辑重建结果
#    Then Result Parameters 应提供 "Relative" 和 "Absolute" 心动相位模式选项
# ------------------------------------------------------

  @step:Absolute_delay_steps/Console-SRS.22778/scenario4
  Scenario Outline: Helical reconstruction image information contains the selected Relative or Absolute indicator
    Given a patient is registered on the Host
    And the "Coronary CTA" protocol is selected
    And Cardiac DoseRight is not selected
    And the scan is performed
    And the final reconstruction is started
    And the reconstruction result is edited in Surview

    When "<cardiac_phase_mode>" is selected in Result Parameters
    And the reconstruction phase value is set to "<recon_phase_value>"
    And the reconstruction is performed
    Then in the View, the four-corner image information should contain the Relative or Absolute indicator consistent with "<recon_phase_value>" in the Examples table

    When Image Parameters is opened
    Then in Image Parameters, the Relative or Absolute indicator should be consistent with "<recon_phase_value>" in the Examples table

  Examples:
      | cardiac_phase_mode | recon_phase_value |
      | absolute           | 300ms            |
      | relative           | 80%              |
# ------------------------------------------------------
#  Scenario Outline: Helical 重建图像信息包含所选的 Relative 或 Absolute 标识
#    Given 已在主机上注册患者
#    And 已选择 "Coronary CTA" 协议
#    And 未选择 Cardiac DoseRight
#    And 已执行扫描
#    And 已启动最终重建
#    And 已在 Surview 中编辑重建结果
#
#    When 在 Result Parameters 中选择 "<心动相位模式>"
#    And 将重建相位值设置为 "<重建相位值>"
#    And 执行重建
#    Then 视图中的四角图像信息应包含与 Examples 表中 "<重建相位值>" 一致的 Relative 或 Absolute 标识
#
#    When 打开 Image Parameters
#    Then Image Parameters 中的 Relative 或 Absolute 标识应与 Examples 表中的 "<重建相位值>" 一致
#
#  Examples:
#      | 心动相位模式 | 重建相位值 |
#      | absolute     | 300ms     |
#      | relative     | 80%       |
# ------------------------------------------------------



  @step:Absolute_delay_steps/Console-SRS.22778/scenario5
  Scenario Outline: DICOM information contains the expected cardiac phase range and unit indicators
    Given reconstructed image data is prepared from Step & Shoot and Helical scans
    And the reconstructed image is generated with the "<cardiac_phase_mode>" option

    When the DICOM Information page is opened
    Then DICOM tag IDs "<tag_id1>", "<tag_id2>", "<tag_id3>", and "<tag_id4>" should contain the value range "<phase_scope>" from the Examples table
    And DICOM tag IDs "<tag_id1>", "<tag_id2>", "<tag_id3>", and "<tag_id4>" should contain the value unit "<phase_unit>" from the Examples table

  Examples:
      | cardiac_phase_mode | phase_scope | phase_unit | tag_id1   | tag_id2   | tag_id3   | tag_id4   |
      | absolute           | 1-1000      | ms         | 0008:103E | 0020:4000 | 00E1:1040 | 01F1:1041 |
      | relative           | 1-100       | %          | 0008:103E | 0020:4000 | 00E1:1040 | 01F1:1041 |
# ------------------------------------------------------
#  Scenario Outline: DICOM 信息包含预期的心动相位范围和单位标识
#    Given 已准备来自 Step & Shoot 和 Helical 扫描的重建图像数据
#    And 已使用 "<心动相位模式>" 选项生成重建图像
#
#    When 打开 DICOM Information 页面
#    Then DICOM Tag ID "<标签ID1>"、"<标签ID2>"、"<标签ID3>" 和 "<标签ID4>" 应包含 Examples 表中的值范围 "<相位范围>"
#    And DICOM Tag ID "<标签ID1>"、"<标签ID2>"、"<标签ID3>" 和 "<标签ID4>" 应包含 Examples 表中的值单位 "<相位单位>"
#
#  Examples:
#      | 心动相位模式 | 相位范围 | 相位单位 | 标签ID1   | 标签ID2   | 标签ID3   | 标签ID4   |
#      | absolute           | 1-1000      | ms         | 0008:103E | 0020:4000 | 00E1:1040 | 01F1:1041 |
#      | relative           | 1-100       | %          | 0008:103E | 0020:4000 | 00E1:1040 | 01F1:1041 |
# ------------------------------------------------------

  @step:Absolute_delay_steps/Console-SRS.22778/scenario6
  Scenario Outline: Review four-corner information contains the expected ECG range and unit indicators
    Given reconstructed image data is prepared from Step & Shoot and Helical scans
    And the reconstructed image is generated with the "<cardiac_phase_mode>" option

    When the Review screen is opened
    Then in Review,the ECG value in the four-corner information should contain the value range "<phase_scope>" from the Examples table
    And in Review,the ECG value in the four-corner information should contain the value unit "<phase_unit>" from the Examples table
    When Image Parameters is opened from the Review screen
    Then Series Description should contain the value range "<phase_scope>" from the Examples table
    And Series Description should contain the value unit "<phase_unit>" from the Examples table

  Examples:
      | cardiac_phase_mode | phase_scope | phase_unit |
      | absolute           | 1-1000      | ms         |
      | relative           | 1-100       | %          |
# ------------------------------------------------------
#  Scenario Outline: Review 四角信息包含预期的 ECG 范围和单位标识
#    Given 已准备来自 Step & Shoot 和 Helical 扫描的重建图像数据
#    And 已使用 "<心动相位模式>" 选项生成重建图像
#
#    When 打开 Review 界面
#    Then Review 四角信息中的 ECG 值应包含 Examples 表中的值范围 "<相位范围>"
#    And Review 四角信息中的 ECG 值应包含 Examples 表中的值单位 "<相位单位>"
#    When 从 Review 界面打开 Image Parameters
#    Then Series Description 应包含 Examples 表中的值范围 "<相位范围>"
#    And Series Description 应包含 Examples 表中的值单位 "<相位单位>"
#
#  Examples:
#      | 心动相位模式 | 相位范围 | 相位单位 |
#      | absolute     | 1-1000   | ms       |
#      | relative     | 1-100    | %        |
# ------------------------------------------------------


  @step:Absolute_delay_steps/Console-SRS.22778/scenario7 #@single:none:8:example=1.1
  Scenario Outline: Quick Review four-corner information contains the expected ECG range and unit indicators
    Given reconstructed image data is prepared from Step & Shoot and Helical scans
    And the reconstructed image is generated with the "<cardiac_phase_mode>" option

    When the Quick Review screen is opened
    Then in Quick Review,the ECG value in the four-corner information should contain the value range "<phase_scope>" from the Examples table
    And in Quick Review,the ECG value in the four-corner information should contain the value unit "<phase_unit>" from the Examples table
    When Image Parameters is opened from the Quick Review screen
    Then Tagging should contain the value range "<phase_scope>" from the Examples table
    And Tagging should contain the value unit "<phase_unit>" from the Examples table

  Examples:
      | cardiac_phase_mode | phase_scope | phase_unit |
      | absolute           | 1-1000      | ms         |
      | relative           | 1-100       | %          |
# ------------------------------------------------------
#  Scenario Outline: Quick Review 四角信息包含预期的 ECG 范围和单位标识
#    Given 已准备来自 Step & Shoot 和 Helical 扫描的重建图像数据
#    And 已使用 "<心动相位模式>" 选项生成重建图像
#
#    When 打开 Quick Review 界面
#    Then Quick Review 四角信息中的 ECG 值应包含 Examples 表中的值范围 "<相位范围>"
#    And Quick Review 四角信息中的 ECG 值应包含 Examples 表中的值单位 "<相位单位>"
#    When 从 Quick Review 界面打开 Image Parameters
#    Then Tagging 应包含 Examples 表中的值范围 "<相位范围>"
#    And Tagging 应包含 Examples 表中的值单位 "<相位单位>"
#
#  Examples:
#      | 心动相位模式 | 相位范围 | 相位单位 |
#      | absolute     | 1-1000   | ms       |
#      | relative     | 1-100    | %        |
# ------------------------------------------------------
