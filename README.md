# workflow-driver

`workflow-driver` 是一个面向工作流编排场景的通用 Python CLI。
它将原先分散在两个脚本里的能力收敛为一个可通过 `uv` 启动的入口，并通过不同子命令执行不同类型的步骤。

当前目录已经是一个独立的 `uv` 项目，适合继续打磨后单独开源。

## 目标

- 单一 CLI 入口，不再让外部用户记忆多个脚本文件
- 支持状态驱动的 workflow 执行
- 支持脚本步骤、模型步骤、最终汇总步骤三类执行能力
- 使用 JSON 文件作为稳定接口，便于自动化系统接入
- 默认兼容 `uv` 运行方式，适合本地使用和 CI 集成

## 目录结构

```text
workflow_driver/
├── pyproject.toml
├── README.md
└── src/workflow_driver/
    ├── __init__.py
    ├── __main__.py
    ├── cli.py
    ├── config.py
    ├── engine.py
    ├── executor.py
    └── utils.py
```

## 安装与运行

如果你在当前目录下开发：

```bash
cd workflow_driver
uv sync
uv run workflow-driver --help
```

如果你在仓库根目录执行：

```bash
uv run --project workflow_driver workflow-driver --help
```

也可以直接按模块运行：

```bash
uv run --project workflow_driver python -m workflow_driver --help
```

## 命令设计

工具现在只有一个统一入口：`workflow-driver`

通过子命令区分能力：

- `workflow`：执行完整工作流状态机，兼容 `start` / `status` / `run` / `next` / `complete` / `run_script_step`
- `script-step`：直接执行一个准备好的脚本步骤
- `model-step`：直接执行一个准备好的模型步骤
- `final-step`：直接执行一个准备好的最终汇总步骤

这就是“一个入口，通过不同命令执行不同工具”的最终形态。

## 最常用示例

### 1. 查看帮助

```bash
uv run --project workflow_driver workflow-driver --help
```

### 2. 执行完整工作流

```bash
uv run --project workflow_driver workflow-driver workflow \
  --workspace /path/to/workspace \
  --input /path/to/request.json \
  --output /path/to/result.json
```

### 3. 直接执行脚本步骤

```bash
uv run --project workflow_driver workflow-driver script-step \
  --workspace /path/to/workspace \
  --input /path/to/step.json \
  --output /path/to/result.json
```

### 4. 直接执行模型步骤

```bash
uv run --project workflow_driver workflow-driver model-step \
  --workspace /path/to/workspace \
  --input /path/to/step.json \
  --output /path/to/result.json
```

## `workflow` 子命令接口

### 输入

`workflow` 接收一个 JSON 请求文件，至少包含：

```json
{
  "action": "run",
  "spec_path": "workflow_spec.yaml",
  "day_id": 20260318,
  "context": {}
}
```

### 可覆盖参数

以下 CLI 参数会覆盖输入文件中的同名字段：

- `--action`
- `--day-id`
- `--run-id`
- `--spec-path`

### 输出

输出为结构化 JSON。
如果指定 `--output`，结果会写入文件；否则输出到 stdout。

## 运行时配置

### 路径相关

- `--workspace`：工作区根目录。默认取当前目录，或环境变量 `WORKFLOW_DRIVER_WORKSPACE`
- `--state-root`：状态与产物根目录。默认是 `<workspace>/tmp`

### 模型网关相关

模型步骤通过 gateway 调用远端会话工具。

可通过以下方式提供配置：

- CLI：`--gateway-url`、`--gateway-token`
- 环境变量：`WORKFLOW_DRIVER_GATEWAY_URL`、`WORKFLOW_DRIVER_GATEWAY_TOKEN`
- 配置文件：`~/.workflow-driver/config.json`

优先级是：CLI > 环境变量 > 配置文件

### 脚本执行命令

默认情况下，脚本步骤会使用如下风格的命令执行：

```bash
uv run --project <project_root> python <script_path> --input <input_path> --output <output_path>
```

如果你的脚本运行时不是这个契约，可以通过：

- CLI: `--script-command-template`
- 环境变量: `WORKFLOW_DRIVER_SCRIPT_COMMAND`

来自定义命令模板。

支持的占位符：

- `{script_path}`
- `{input_path}`
- `{output_path}`
- `{workspace}`
- `{project_root}`

例如如果你想接入自定义脚本执行包装器：

```bash
export WORKFLOW_DRIVER_SCRIPT_COMMAND='python {script_path} --input {input_path} --output {output_path}'
```

## 设计说明

### 1. 为什么要改成单入口

原实现里 `workflow_driver.py` 和 `workflow_executor.py` 都承担了对外职责，使用者需要理解两个脚本的边界。
现在对外只有 `workflow-driver` 一个命令，内部再按模块拆分，外部心智负担更小。

### 2. 为什么保留 JSON 文件接口

工作流驱动器通常会被上层系统、调度器或其他 agent 调用。
相比 stdin 拼接或命令行长参数，JSON 文件接口更稳定、更容易审计，也方便保留运行痕迹。

### 3. 为什么保留模块化内部结构

虽然对外只有一个入口，但内部仍按职责拆分：

- `cli.py` 负责命令行入口
- `engine.py` 负责工作流状态机
- `executor.py` 负责脚本/模型/最终步骤执行
- `config.py` 负责运行时配置与路径解析
- `utils.py` 负责通用 IO 与时间工具

这能兼顾“单入口”和“可维护性”。

## 开源前建议

当前目录已经具备独立发布的基础，但在正式开源前，建议再补齐以下项目级事项：

- 在 `pyproject.toml` 中补充真实的项目主页、仓库和问题反馈地址
- 明确选择许可证，并补充 `LICENSE`
- 如果要发布到 PyPI，确认项目名 `workflow-driver` 是否可用
- 增加 CI，如 `ruff`、`pytest`、`python -m build`
- 增加端到端示例与最小可运行 workflow spec

## 发布建议

本地验证通过后，可以考虑使用以下流程：

```bash
cd workflow_driver
uv sync
uv run workflow-driver --help
uv build
```

如果后续你愿意，我可以继续帮你补：

- `LICENSE`
- `pytest` 测试
- `ruff` / `mypy` 配置
- GitHub Actions 发布与检查流程
