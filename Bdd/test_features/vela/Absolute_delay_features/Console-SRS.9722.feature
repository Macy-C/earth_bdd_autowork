# SRS ID: CT-NM.SRS.Modes.2214  CT-NM.SRS.Modes.2215
# SSRS ID: Console-SRS.9722
# SSRS Description:
# The Console shall reflect the selection of the phase tolerance on the irradiation profile display on the wave in the ECG Online Viewer
# (during plan, during acquisition and after acquisition was completed).
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-06-10
# Updated Date: 2026-08-03
# Version: V2

@skip @不适合自动化
Feature: Console-SRS.9722
  The Console shall reflect the selected phase tolerance on the irradiation profile display in the ECG Online Viewer
  so that the configured phase and phase tolerance can be visually confirmed against the ECG waveform during planning, acquisition, and after acquisition is completed.

  Rule: During planning, the Console shall reflect the selected phase tolerance on the irradiation profile display over the ECG waveform in the ECG Online Viewer.
  Rule: During acquisition, the Console shall reflect the selected phase tolerance on the irradiation profile display over the ECG waveform in the ECG Online Viewer.
  Rule: After acquisition is completed, the Console shall reflect the selected phase tolerance on the irradiation profile display over the ECG waveform in the ECG Online Viewer.

  Background:
    Given a patient is registered on the Console
    And the "Step & Shoot" protocol is selected
    And the clinical sequence parameters are open on the Display Interface
    And the cardiac phase mode is set to "absolute" on the Display Interface

  Scenario: Irradiation profile reflects phase tolerance during planning
    When the phase and phase tolerance are configured on the Display Interface
    Then the ECG Online Viewer shows the configured phase on the ECG waveform during planning
    And the irradiation profile display reflects the selected phase tolerance around the configured phase during planning
# ------------------------------------------------------
#  Scenario: 照射曲线在计划阶段反映相位容差
#    When 在显示界面上配置相位和相位容差
#    Then ECG Online Viewer 在计划阶段的 ECG 波形上显示已配置的相位
#    And 照射曲线显示在计划阶段围绕已配置相位反映所选的相位容差
# ------------------------------------------------------

  Scenario: Irradiation profile reflects phase tolerance during acquisition
    Given the phase and phase tolerance are configured on the Display Interface
    When scan acquisition is in progress
    Then the ECG Online Viewer shows the configured phase on the ECG waveform during acquisition
    And the irradiation profile display reflects the selected phase tolerance around the configured phase during acquisition
# ------------------------------------------------------
#  Scenario: 照射曲线在采集过程中反映相位容差
#    Given 已在显示界面上配置相位和相位容差
#    When 扫描采集正在进行中
#    Then ECG Online Viewer 在采集过程中的 ECG 波形上显示已配置的相位
#    And 照射曲线显示在采集过程中围绕已配置相位反映所选的相位容差
# ------------------------------------------------------

  Scenario: Irradiation profile reflects phase tolerance after acquisition is completed
    Given the phase and phase tolerance are configured on the Display Interface
    When scan acquisition is completed
    Then the ECG Online Viewer shows the configured phase on the ECG waveform after acquisition is completed
    And the irradiation profile display reflects the selected phase tolerance around the configured phase after acquisition is completed
# ------------------------------------------------------
#  Scenario: 照射曲线在采集完成后反映相位容差
#    Given 已在显示界面上配置相位和相位容差
#    When 扫描采集已完成
#    Then ECG Online Viewer 在采集完成后的 ECG 波形上显示已配置的相位
#    And 照射曲线显示在采集完成后围绕已配置相位反映所选的相位容差
# ------------------------------------------------------
