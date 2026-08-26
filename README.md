# BDD Autowork

Windows 桌面应用 BDD 自动化框架，基于 `behave`、`pywinauto`、Airtest 图片识别和 PaddleOCR。

框架目标是：feature 描述业务，Page Object 承载业务动作，locator/data 配置化管理，底层 actions 专注通用 UI 行为。

## 快速运行

建议从项目根目录使用模块方式运行。

正式用例：

```powershell
python -m Bdd.runner
```

本地调试：

```powershell
python -m Bdd.local_runner
```

指定 feature 调试：

```powershell
python -m Bdd.local_runner Bdd/test_features/test_debug.feature
```

在 VS Code 多根工作区中，请把 `bdd_autowork` 作为独立工作区文件夹加入。仓库内 `.vscode` 配置会为右键运行和调试当前 Python 文件注入项目根，使深层文件中的 `Bdd`、`autowork_core` 和 `config` 导入正常工作。

安装、输出格式和调试方式见 [1.快速开始](docs/1.快速开始.md)。

## 使用文档

| 文档 | 内容 |
| --- | --- |
| [docs/1.快速开始.md](docs/1.快速开始.md) | 安装、运行、本地调试和输出 |
| [docs/2.编写自动化脚本.md](docs/2.编写自动化脚本.md) | Feature、Step、Page/View、Recorder 和 AI 生成 |
| [docs/3.动作参考.md](docs/3.动作参考.md) | BasePage 动作签名、等待和断言 |
| [docs/4.定位器与配置.md](docs/4.定位器与配置.md) | locator、data、Root 和运行配置 |
| [docs/5.故障排查.md](docs/5.故障排查.md) | 日志、Recorder、视觉定位和依赖问题 |
| [docs/6.版本与升级说明.md](docs/6.版本与升级说明.md) | 当前版本和最近三个版本的升级影响 |
| [docs/维护/1.维护者指南.md](docs/维护/1.维护者指南.md) | 框架架构、协议和 AI 治理入口 |
| [ai/README.md](ai/README.md) | **AI 资产**：Context、Prompt、Knowledge 与协作复盘 |

## 目录概览

```text
Bdd/                 # 用户侧资产：config、features、steps、page_obj、locators、data
autowork_core/       # 框架内核：actions、common、page、runtime、utils
config/              # 框架配置加载代码和路径常量
resources/           # OCR 模型、Allure 等资源
artifacts/           # logs、reports、screenshots 输出产物
docs/                # 框架详细文档
ai/                  # 版本化 AI 上下文、Prompt 和本地知识
.github/             # VS Code Copilot 自动发现入口与 Hook
framework_validation/ # 仅框架开发/发布使用，不同步宿主项目
```

## 编写脚本

- feature 写业务，不堆 UI 细节。
- step 保持轻薄，只调用 Page Object。
- Page Object 写业务方法，locator/data 放 YAML。

完整流程见 [2.编写自动化脚本](docs/2.编写自动化脚本.md)。动作与定位配置分别以
[3.动作参考](docs/3.动作参考.md) 和 [4.定位器与配置](docs/4.定位器与配置.md) 为准；
Recorder 内部协议见 [维护/3.Recorder设计](docs/维护/3.Recorder设计.md)。
