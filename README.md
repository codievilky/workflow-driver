# workflow-driver

`workflow-driver` 是一个面向 workflow spec 的通用 Python CLI。

它现在采用单一入口设计：

- 只保留一个公开命令：`workflow-driver run`
- 支持指定目标步骤 `--step-number`
- 支持基于 `input_sources` 自动回溯依赖
- 支持复用 `data-dir` 下已有产物
- 模型步骤直接把真实 JSON 输入内联进 prompt，而不是只传一个本地文件路径

## 安装

在项目目录下：

```bash
cd workflow_driver
uv sync
uv run workflow-driver --help
```

也可以直接按模块运行：

```bash
uv run python -m workflow_driver --help
```

## 命令设计

公开命令只有一个：

```bash
workflow-driver run
```

常用参数：

- `--spec`：workflow yaml 路径，必填
- `--workspace`：工作区根目录，默认使用 `--spec` 对应 yaml 所在目录
- `--data-dir`：产物目录，默认 `<workspace>/tmp`
- `--step-number`：目标步骤号；不填则执行到最后一步
- `--day-id`：常用状态参数 `day_id`
- `--state key=value`：补充任意 state 参数，值支持 JSON
- `--context key=value`：补充任意 context 参数，值支持 JSON
- `--output`：将本次执行结果写到 JSON 文件
- `--force`：忽略现有产物并强制重跑
- `--gateway-url` / `--gateway-token`：模型步骤网关配置

## 执行语义

### 1. 指定目标步骤

- 不指定 `--step-number`：从依赖角度执行到最后一步
- 指定 `--step-number 2`：只保证第 2 步产物可用
- 如果目标步骤依赖上游产物，driver 会自动回溯执行依赖步骤

### 2. 产物复用

driver 会在 `data-dir` 中查找步骤默认产物名：

- 如果目标步骤产物已存在，默认直接复用
- 如果目标步骤缺失，但依赖步骤产物存在，则只执行缺失的那一步
- 如果依赖也缺失，则递归回溯生成

### 3. 模型步骤输入

对于 `model` 步骤，driver 会：

1. 根据 `input_sources` 解析真实输入
2. 如果步骤定义了 `input_builder`，先运行 builder 生成模型输入
3. 将最终 JSON 输入直接内联到 prompt 中
4. 再调用模型

这意味着模型不再依赖“自己去读本地 JSON 文件路径”。

## 路径解析规则

- 如果显式传了 `--workspace`，则 `--spec` 相对 `--workspace` 解析
- 如果未传 `--workspace`，则默认将 `workflow_spec.yaml` 所在目录视为 workspace
- workflow yaml 内的执行相关路径相对 yaml 文件所在目录解析

例如当 `workflow_spec.yaml` 位于项目根目录时：

```yaml
input_builder: scripts/build_emotion_regime_input.py
normalizer: scripts/normalize_emotion_regime_output.py

execution:
  project_root: .
```

上面这些路径都会以 `workflow_spec.yaml` 所在目录作为基准目录解析。

## 最常用示例

### 1. 查看帮助

```bash
uv run workflow-driver run --help
```

### 2. 执行整个 workflow

```bash
uv run workflow-driver run --spec /path/to/workspace/workflow_spec.yaml --day-id 20260318 --output /path/to/run-result.json
```

### 3. 只执行到第 2 步

```bash
uv run workflow-driver run --spec /path/to/workspace/workflow_spec.yaml --data-dir /path/to/tmp/run-20260318 --step-number 2 --day-id 20260318 --output /path/to/step2-result.json
```

### 4. 复用已有 step1 产物，只补跑 step2

如果 `data-dir` 中已经有：

- `step1_day_summary_packet.json`

那么执行：

```bash
uv run workflow-driver run --spec /path/to/workspace/workflow_spec.yaml --data-dir /path/to/tmp/run-20260318 --step-number 2 --day-id 20260318
```

driver 会自动复用 `step1`，只执行 `step2`。

## 输入参数来源

### state

可以通过以下方式提供 state 参数：

- `--day-id 20260318`
- `--state market="cn-a"`
- `--state extra='{"foo": 1}'`
- `--state-file /path/to/state.json`

### context

可以通过以下方式提供 context 参数：

- `--context mainline_concepts='["算力", "机器人"]'`
- `--context unstable_themes='["高位抱团"]'`
- `--context-file /path/to/context.json`

只有目标步骤实际依赖到的 state/context 才必须提供；未用到的参数可以省略。

## 输出结果

`run` 的输出是一个结构化 JSON，主要包含：
默认输出内容就是目标步骤的最终产物内容：

- `script` 步骤：脚本产物 JSON
- `model` 步骤：`normalizer` 之后的 JSON；如果没有 `normalizer`，则直接输出模型 JSON
- `final` 步骤：final step 产物 JSON

执行过程中的步骤播报、读取文件、生成文件信息会输出到 `stderr`，不会混入默认 JSON 输出。

## 模型网关配置

模型步骤通过 gateway 调用远端会话工具。

可通过以下方式提供配置：

- CLI：`--gateway-url`、`--gateway-token`
- 环境变量：`WORKFLOW_DRIVER_GATEWAY_URL`、`WORKFLOW_DRIVER_GATEWAY_TOKEN`
- 配置文件：`~/.workflow-driver/config.json`

优先级：CLI > 环境变量 > 配置文件

配置文件示例：

```json
{
  "gateway": {
    "url": "http://localhost:18789/tools/invoke",
    "auth": {
      "token": "your-token"
    }
  }
}
```

兼容旧格式：

```json
{
  "gateway": {
    "port": 18789,
    "auth": {
      "token": "your-token"
    }
  }
}
```

## 脚本执行契约

本地脚本步骤仍然通过显式 `--input/--output` 调用脚本，但这是 driver 的内部实现，不再要求调用方自己准备外部 JSON 请求文件。

默认脚本执行风格：

```bash
uv run --project <project_root> python <script_path> --input <input_path> --output <output_path>
```

如果你需要接入自定义执行包装器，可以通过：

- CLI：`--script-command-template`
- 环境变量：`WORKFLOW_DRIVER_SCRIPT_COMMAND`

例如：

```bash
export WORKFLOW_DRIVER_SCRIPT_COMMAND='python {script_path} --input {input_path} --output {output_path}'
```

## 当前状态

目前这版已经支持：

- 单命令 `run`
- `--step-number` 单步目标执行
- `data-dir` 产物复用
- 依赖回溯
- model step 真实 JSON 内联 prompt

## 后续建议

如果准备继续开源，建议补齐：

- `LICENSE`
- `pytest` smoke tests
- `ruff` / `mypy`
- GitHub Actions CI
- 一个最小可运行 demo spec
