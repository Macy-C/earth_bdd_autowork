# 项目 AI 资产

`Bdd/ai/`只保存当前项目拥有的AI资产。更新BDD Autowork框架时必须完整保留该目录。

```text
Bdd/ai/
  portable-knowledge/    # 经净化和确认后可随项目Git提交的知识
  knowledge/             # 本地长期Knowledge，默认由.gitignore排除
```

## 所有权

- `portable-knowledge/records`只接收production且用户确认的Capability；记录绑定当前
  项目仓库并保持内容寻址、不可变和advisory。
- `knowledge/`由Recorder、协作Review和Shadow Companion自动维护，可能包含产品业务
  信息，默认不进入Git。迁移机器时应先审计，再通过受控存储迁移需要保留的内容。

Request、Brief、Decision、Plan、Transaction、Workflow、录屏、截图、报告和UI tree
属于单次运行产物，保存在`artifacts/`，不属于这里，也不通过普通Git迁移。

顶层`ai/`保存框架拥有的Context、Prompt、Instruction、Schema和Policy；它可以随框架
版本完整替换。