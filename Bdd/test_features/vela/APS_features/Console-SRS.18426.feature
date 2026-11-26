# SRS ID: CT-NM.SRS.SysPerf.4807  CT-NM.SRS.SysPerf.4808
# SSRS ID: Console-SRS.18426
# SSRS Description:
#      The Console shall get the ready to reconstruct images in 2.8sec. or less (with one Surview, two clinical acquisitions and 12 CT results).
#      Operation start: User pressed "Start Final Recon"
#      Operation end: GetReady request is sent (MDU built and sent)
#      Exceptions: All realtime images, CCT, surview and preview images.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-07-27
# Updated Date: 2026-07-27
# Version: V1

@skip @性能 @手工
Feature: Console-SRS.18426
      The Console shall get the ready to reconstruct images in 2.8sec. or less (with one Surview, two clinical acquisitions and 12 CT results).
      Operation start: User pressed "Start Final Recon"
      Operation end: GetReady request is sent (MDU built and sent)
      Exceptions: All realtime images, CCT, surview and preview images.
      控制台应在 2.8 秒或更短时间内获取准备重建的图像（包括 1 个 Surview、2 次临床采集和 12 个 CT 结果）。
      操作开始：用户按下“Start Final Recon”
      操作结束：发送 GetReady 请求（MDU 已构建并发送）
      例外：所有实时图像、CCT、surview 和 preview 图像。

# ------------------------------------------------------
# 中文备注：
# 本 Feature 验证控制台在包含 1 个 Surview、2 次临床采集和 12 个 CT 结果时，
# 应在 2.8 秒内完成待重建图像的准备。
# 性能计时起点：用户按下 "Start Final Recon"。
# 性能计时终点：发送 GetReady 请求，即 MDU 构建并发送完成。
# 不适用范围：所有实时图像、CCT、Surview 和 Preview 图像。
# ------------------------------------------------------
