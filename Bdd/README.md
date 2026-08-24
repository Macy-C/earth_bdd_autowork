# BDD Autowork 参考宿主

`Bdd/`是框架发布源中的中性宿主骨架。实际产品项目拥有自己的Feature、Step、
Page Object、locator、data、应用生命周期、外部集成和项目AI资产；这些内容不应进入
`autowork_core/`或框架发布源的参考宿主。

## 目录职责

- `test_features/`：本地调试Feature；正式项目可另建`features/`。
- `steps/`：Behave Step definitions。
- `page_obj/`：项目Page Object与WindowView。
- `locators/`：项目locator YAML。
- `data/`：项目data YAML及图片资源；`public_data.yaml`必须存在。
- `application.py`：可选的项目应用生命周期Hook模板。
- `ai/`：当前宿主拥有的项目知识和质量Oracle，不随框架更新而替换。

## 运行

在项目根目录安装依赖：

```powershell
python -m pip install -r requirements.txt
```

正式运行：

```powershell
python -m Bdd.runner
```

本地调试：

```powershell
python -m Bdd.local_runner [feature路径]
```

## 应用生命周期

应用行为由`config/config.yaml`中的`APP_LAUNCH_MODE`和`APP_SETTING.APP_PATH`决定：

- `attach`连接并保留已有应用，不调用应用启停Hook。
- `auto`配合应用路径时，由框架启动并清理应用进程。
- `auto`配合`APP_PATH: runtime`时，宿主必须实现`application.py`中的
  `start_application()`和`stop_application()`；模板默认失败关闭。
- `prepare_scenario(context, scenario)`和`cleanup_scenario(context, scenario)`是可选的
  场景资源Hook，模板默认不执行任何操作。

不需要自定义生命周期时，可以删除`application.py`，但不得将`APP_PATH`配置为
`runtime`。

## 项目AI资产

`Bdd/ai/`属于宿主项目。框架发布源只保留目录说明、忽略规则和空Oracle注册表；不得提交
本地Knowledge、录制证据、运行报告或产品业务事实。详见[项目AI资产](ai/README.md)。

## 更新框架

从框架仓库拉取目标版本后，完整替换本项目中的框架资产目录和根文件；保留整个`Bdd/`、
`config/`和`artifacts/`。项目开发不直接修改`autowork_core/`、顶层`ai/`、`docs/`、
`.github/`或`resources/`。`framework_validation/`不复制到项目仓库。替换后检查Git diff并
运行轻量验证，再在当前项目仓库提交：

```powershell
python -B -m autowork_core.utils.debug_tools.framework_smoke --project-root .
```

发布源维护者另外运行框架验证套件：

```powershell
python -B -m unittest discover -s framework_validation/tests -p "test_*.py"
```