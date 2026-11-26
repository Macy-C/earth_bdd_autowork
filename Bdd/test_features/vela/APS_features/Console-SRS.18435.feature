# SRS ID: CT-NM.SRS.SysPerf.4807  CT-NM.SRS.SysPerf.4808
# SSRS ID: Console-SRS.18435
# SSRS Description:
#     The Console shall get the ready to reconstruct images in 2.8sec. or less (with one Surview, two clinical acquisitions and 12 CT results).
#     Operation start: User pressed "Start Final Recon"
#     Operation end: GetReady request is sent (MDU built and sent)
#     Exceptions: All realtime images, CCT, surview and preview images.
# Author: TBD
# Business Reviewer: TBD
# Test Reviewer: TBD
# Development Reviewer: TBD
# Feature Status: Draft
# Created Date: 2026-07-27
# Updated Date: 2026-07-27
# Version: V1

@skip @性能 @手工
Feature: Console-SRS.18435
      The Console shall get the ready to reconstruct images in 2.8sec. or less (with one Surview, two clinical acquisitions and 12 CT results).
      Operation start: User pressed "Start Final Recon"
      Operation end: GetReady request is sent (MDU built and sent)
      Exceptions: All realtime images, CCT, surview and preview images.
      主机平均应在 0.18 秒（180ms）内显示 1 幅常规轴位图像（平均渲染模式），适用于关注的各协议，容差为 5%：
      操作开始：从 CIRS 接收到图像数据
      操作结束：屏幕上显示常规图像
      例外：所有实时图像、CCT、surview 和 preview 图像

# ------------------------------------------------------
# 中文备注：
# 本 Feature 的中文需求目标为：对于关注的各协议，主机平均应在 0.18 秒（180 ms）内
# 显示 1 幅常规轴位图像（平均渲染模式），允许 5% 的容差。
# 性能计时起点：从 CIRS 接收到图像数据。
# 性能计时终点：常规图像显示在屏幕上。
# 不适用范围：所有实时图像、CCT、Surview 和 Preview 图像。
# 注意：当前英文 Feature 描述仍为“2.8 秒内准备重建图像”，与上述中文目标不一致，待确认。
# ------------------------------------------------------
