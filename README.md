# Mini Coding Agent

> A small local coding agent with CLI, persistent runs, MCP examples, and a lightweight web console.

Mini Coding Agent (`mca`) 是一个面向本地开发和 Agent 实验的小型编码智能体。它支持命令行交互、会话持久化、上下文管理、任务状态记录、评测脚本和 MCP 示例服务，适合用来验证 coding agent 的工具调用、记忆、评测和安全边界。

## Highlights

- **Local-first coding agent**: 通过 CLI 启动，适合在本地工作区内进行编码任务实验。
- **Multi-provider model support**: 支持 Ollama、OpenAI-compatible、Anthropic-compatible 和 DeepSeek 风格模型配置。
- **Persistent run store**: 保存每次运行的任务状态、上下文和执行记录。
- **MCP examples**: 内置 math、notes 等 MCP server 示例，便于测试工具协议。
- **Evaluation utilities**: 提供 benchmark、metrics、large-scale experiment scripts。
- **Web console**: `web/` 提供 React/Vite 控制台，用于观察和操作 agent runs。

## Demo

CLI 帮助信息：

![mca help](assets/screenshots/mca-help.png)

启动界面：

![mca start](assets/screenshots/mca-start.png)

REPL 内置命令与会话路径：

![mca repl](assets/screenshots/mca-repl.png)

## How It Works

```text
User / CLI / Web Console
        |
        v
mca runtime
        |
        +--> context manager
        +--> workspace tools
        +--> memory and run store
        +--> MCP tool clients
        |
        v
LLM provider
```

Agent 运行时读取用户任务和工作区上下文，按配置调用模型，并将任务状态、上下文窗口和工具结果写入本地 run store。MCP 示例服务用于验证外部工具协议，不默认执行高风险系统操作。

## Features

| Module | Description |
|---|---|
| **CLI Runtime** | `mca` 命令行入口，支持交互式 agent 会话。 |
| **Context Manager** | 管理当前任务需要的文件、上下文和裁剪策略。 |
| **Memory** | 保存会话和任务相关信息，便于后续恢复与评估。 |
| **Task State** | 记录任务步骤、状态和运行历史。 |
| **Metrics & Evaluator** | 提供 coding task benchmark 与结果统计。 |
| **MCP Examples** | `examples/` 中包含可运行的 MCP server 示例。 |
| **Web Console** | React/Vite 前端用于查看和操作 runs。 |

## Tech Stack

- **Core**: Python 3.10+, FastAPI, prompt-toolkit, PyYAML
- **Agent Tools**: MCP Python SDK, local workspace utilities
- **Frontend**: React, Vite, TypeScript, lucide-react
- **Testing**: pytest, ruff

## Quick Start

```bash
git clone https://github.com/m4rklee/mini-coding-agent.git
cd mini-coding-agent
python -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest ruff
```

配置模型环境变量：

```bash
cp .env.example .env
```

启动 CLI：

```bash
mca
```

或直接通过模块运行：

```bash
python -m mca
```

## Web Console

```bash
cd web
npm install
npm run dev
```

## Usage

运行 MCP 示例：

```bash
python examples/mcp_math_server.py
python examples/mcp_notes_server.py
```

运行测试：

```bash
pytest -q
```

运行实验脚本：

```bash
python scripts/run_provider_experiments.py
python scripts/run_large_scale_experiments.py
```

## Project Structure

```text
mca/                    # CLI, runtime, memory, tools, metrics, evaluator
examples/               # MCP server examples
benchmarks/             # Coding task benchmark data
scripts/                # Experiment and metric collection scripts
tests/                  # Unit and safety tests
web/                    # React/Vite web console
docs/                   # Flow diagrams and supporting docs
```

## Safety & Limitations

- 这是本地 coding agent 实验项目，不建议直接授予生产环境写权限。
- MCP 示例用于协议验证，不代表完整权限隔离系统。
- 不同模型 provider 的行为差异较大，建议用 benchmark 和 safety tests 持续验证。

## Roadmap

- Add richer tool permission policies.
- Improve web console run inspection.
- Add more reproducible coding benchmarks.
- Expand model-provider comparison reports.
