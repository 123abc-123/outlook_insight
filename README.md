# 认知雷达接口原型

本项目实现两个接口：`POST /decompose` 和 `POST /update`。设计和配置说明见 [第一版简要说明](docs/00_第一版简要说明.md)。

## 启动

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[test]"
Copy-Item config/radar.toml config/radar.local.toml
# 编辑 radar.local.toml：将 model_mode 改为 live，并填写内网 URL 和模型名
# 把认知报告 MD 文件放到 radar.local.toml 中 reports.root_dir 指定的目录
$env:RADAR_CONFIG = "config/radar.local.toml"
$env:RADAR_LLM_API_KEY = "公司提供的 Key"
.venv\Scripts\python.exe -m uvicorn radar.main:app --host 0.0.0.0 --port 8000
```

打开 `http://127.0.0.1:8000/docs` 查看请求结构。默认配置是 `unconfigured`，因此不会意外向示例 URL 发送内部材料。

`/update` 的 `report_path` 使用相对于 `reports.root_dir` 的路径。首次调用保存 v1，成功更新后新建 v2 和对应的变更说明文件；后续自动基于最新版本继续更新。

## 验证

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m radar.demo
```

演示模式只处理仓库内置的虚构案例，用于验证接口流程和版本更新，不代表真实模型效果。
