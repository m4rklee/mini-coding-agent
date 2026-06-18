"""命令行入口。

这个模块负责把“用户怎么启动 mca”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import os
import shlex
import sys
from argparse import Namespace

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML

from .config import load_project_env, provider_env
from .models import AnthropicCompatibleModelClient, OllamaModelClient, OpenAICompatibleModelClient
from .runtime import Mca, SessionStore
from .workspace import WorkspaceContext

# 模型环境变量配置
DEFAULT_SECRET_ENV_NAMES = (
    "MCA_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "MCA_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "MCA_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "MCA_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)
# 模型固定的启动显示文本
WELCOME_NAME = "mca"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "calm shell, ready for work"
_ACTION_EXIT = object()

_COMMANDS = [
    ("/help", "Show this help message.", lambda agent: print(HELP_DETAILS)),
    ("/plan", "Enter plan mode.", lambda agent: (setattr(agent, "mode", "plan"), agent.refresh_prefix(force=True))[-1]),
    ("/react", "Enter ReAct mode.", lambda agent: (setattr(agent, "mode", "ReAct"), agent.refresh_prefix(force=True))[-1]),
    ("/model", "Show or update the active model.", lambda agent: _handle_model_command(agent, "/model")),
    ("/skill", "Show available skills.", lambda agent: print(_format_skill_list(agent))),
    ("/memory", "Show the agent's distilled working memory.", lambda agent: print(agent.memory_text())),
    ("/session", "Show the path to the saved session file.", lambda agent: print(agent.session_path)),
    ("/reset", "Clear the current session history and memory.", lambda agent: (agent.reset(), print("session reset"))[-1]),
    ("/exit", "Exit the agent.", lambda agent: _ACTION_EXIT),
    ("/quit", "Exit the agent.", lambda agent: _ACTION_EXIT),
]

HELP_DETAILS = "Commands:\n" + "\n".join(
    f"  {name:<8} {desc}" for name, desc, _ in _COMMANDS
)


class _SlashCommandCompleter(Completer):
    """当输入以 '/' 开头时，提供命令补全。"""

    def get_completions(self, document, complete_event):
        text = document.text
        if not text.startswith("/"):
            return
        for name, desc, _handler in _COMMANDS:
            if name.startswith(text):
                yield Completion(
                    name,
                    start_position=-len(text),
                    display=name,
                    display_meta=desc,
                )


# Agent 默认模型设置
DEFAULT_OLLAMA_MODEL = "qwen3.5:4b"
DEFAULT_OLLAMA_HOST = "http://127.0.0.1:11434"
DEFAULT_OPENAI_MODEL = "gpt-5.4"
DEFAULT_OPENAI_BASE_URL = "https://www.right.codes/codex/v1"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-6"
DEFAULT_ANTHROPIC_BASE_URL = "https://www.right.codes/claude/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-pro"
DEFAULT_DEEPSEEK_MODEL_FOR_MODEL_COMMAND = "deepseek-v4-flash"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
LEGACY_SECRET_ENV_NAMES_VAR = "MINI_CODING_AGENT_SECRET_ENV_NAMES"
SECRET_ENV_NAMES_VAR = "MCA_SECRET_ENV_NAMES"
MODEL_PROVIDERS = ("ollama", "openai", "anthropic", "deepseek")
MODEL_USAGE = (
    "Usage:\n"
    "  /model\n"
    "  /model show\n"
    "  /model set provider=<ollama|openai|anthropic|deepseek> model=<name>\n"
    "  /model set provider=<ollama|openai|anthropic|deepseek>\n"
    "  /model set model=<name>"
)
MODEL_ARG_FIELDS = (
    "cwd",
    "provider",
    "model",
    "base_url",
    "host",
    "ollama_timeout",
    "openai_timeout",
    "temperature",
    "top_p",
    "resume",
    "approval",
    "secret_env_names",
    "max_steps",
    "max_new_tokens",
    "enable_mcp",
)


def _format_skill_list(agent):
    skills = getattr(agent, "skills", None) or {}
    if not skills:
        return "No skills available."
    lines = ["Skills:"]
    for name, skill in sorted(skills.items()):
        description = str(skill.get("description", "")).strip()
        if description:
            lines.append(f"  {name:<16} {description}")
        else:
            lines.append(f"  {name}")
    return "\n".join(lines)


# 模型选择
def _effective_model(args, provider):
    # 模型选择优先级：
    # 1. 用户显式传入 --model
    # 2. provider 对应的环境变量
    # 3. 代码里的默认值
    explicit_model = getattr(args, "model", None)
    if explicit_model:
        return explicit_model
    if provider == "openai":
        model = provider_env("MCA_OPENAI_MODEL", ("OPENAI_MODEL",))
        if model:
            return model
        return DEFAULT_OPENAI_MODEL
    if provider == "anthropic":
        model = provider_env("MCA_ANTHROPIC_MODEL", ("ANTHROPIC_MODEL",))
        if model:
            return model
        return DEFAULT_ANTHROPIC_MODEL
    if provider == "deepseek":
        model = provider_env("MCA_DEEPSEEK_MODEL", ("DEEPSEEK_MODEL",))
        if model:
            return model
        return DEFAULT_DEEPSEEK_MODEL
    return DEFAULT_OLLAMA_MODEL


# 收集隐私信息，用于后续日志脱敏和隐私保护
def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if not extra_names.strip():
        extra_names = os.environ.get(LEGACY_SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper()
            for item in extra_names.split(",")
            if item.strip()
        )
    return sorted(configured_secret_names)


# 根据不同模型的API请求方式创建不同模型
def _build_model_client(args):
    provider = getattr(args, "provider", "openai")
    # CLI 只负责把 provider 选择翻译成具体 client。
    # 真正的提示词格式、缓存支持、HTTP 协议差异，都封装在 models.py 里。
    if provider == "openai":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("MCA_OPENAI_API_BASE", ("OPENAI_API_BASE",), DEFAULT_OPENAI_BASE_URL)
        api_key = provider_env("MCA_OPENAI_API_KEY", ("OPENAI_API_KEY",))
        return OpenAICompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "anthropic":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("MCA_ANTHROPIC_API_BASE", ("ANTHROPIC_API_BASE",), DEFAULT_ANTHROPIC_BASE_URL)
        api_key = provider_env(
            "MCA_ANTHROPIC_API_KEY",
            ("ANTHROPIC_API_KEY", "MCA_RIGHT_CODES_API_KEY", "RIGHT_CODES_API_KEY", "MCA_OPENAI_API_KEY", "OPENAI_API_KEY"),
        )
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )
    if provider == "deepseek":
        model = _effective_model(args, provider)
        base_url = getattr(args, "base_url", None) or provider_env("MCA_DEEPSEEK_API_BASE", ("DEEPSEEK_API_BASE",), DEFAULT_DEEPSEEK_BASE_URL)
        api_key = provider_env("MCA_DEEPSEEK_API_KEY", ("DEEPSEEK_API_KEY",))
        return AnthropicCompatibleModelClient(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", getattr(args, "ollama_timeout", 300)),
        )

    model = _effective_model(args, provider)
    host = getattr(args, "host", DEFAULT_OLLAMA_HOST)
    return OllamaModelClient(
        model=model,
        host=host,
        temperature=args.temperature,
        top_p=args.top_p,
        timeout=args.ollama_timeout,
    )


def _copy_args_with_model(args, provider, model):
    values = dict(vars(args))
    values["provider"] = provider
    values["model"] = model
    return Namespace(**values)


def _snapshot_args(args):
    values = {}
    for field in MODEL_ARG_FIELDS:
        if hasattr(args, field):
            values[field] = getattr(args, field)
    return Namespace(**values)


def _model_endpoint(client):
    values = getattr(client, "__dict__", {})
    if values.get("host"):
        return values["host"]
    if values.get("base_url"):
        return values["base_url"]
    return getattr(client, "host", getattr(client, "base_url", ""))


def _attach_model_config(agent, args):
    provider = getattr(args, "provider", "openai")
    agent.model_config = {
        "provider": provider,
        "model": _effective_model(args, provider),
        "base_args": _snapshot_args(args),
    }
    return agent


def _apply_agent_mode(agent, args):
    mode = getattr(args, "mode", None)
    if not mode:
        return agent
    agent.mode = mode
    agent.refresh_prefix(force=True)
    return agent


def _format_model_status(agent):
    config = getattr(agent, "model_config", {}) or {}
    provider = config.get("provider", "")
    model = getattr(agent.model_client, "model", config.get("model", ""))
    client = agent.model_client.__class__.__name__
    endpoint = _model_endpoint(agent.model_client)
    lines = [
        "Model:",
        f"  provider: {provider}",
        f"  model: {model}",
        f"  client: {client}",
    ]
    if endpoint:
        lines.append(f"  endpoint: {endpoint}")
    return "\n".join(lines)


def _parse_key_value_tokens(tokens):
    parsed = {}
    for token in tokens:
        if "=" not in token:
            raise ValueError(f"expected key=value, got: {token}")
        key, value = token.split("=", 1)
        key = key.strip().lower().replace("_", "-")
        value = value.strip()
        if key not in {"provider", "model"}:
            raise ValueError(f"unknown model option: {key}")
        if not value:
            raise ValueError(f"empty value for model option: {key}")
        parsed[key] = value
    return parsed


def _handle_model_command(agent, user_input):
    try:
        parts = shlex.split(user_input)
    except ValueError as exc:
        print(f"model command error: {exc}")
        print(MODEL_USAGE)
        return None

    if len(parts) == 1 or parts[1] == "show":
        print(_format_model_status(agent))
        return None

    if parts[1] != "set":
        print(MODEL_USAGE)
        return None

    try:
        updates = _parse_key_value_tokens(parts[2:])
    except ValueError as exc:
        print(f"model command error: {exc}")
        print(MODEL_USAGE)
        return None

    if not updates:
        print(MODEL_USAGE)
        return None

    config = getattr(agent, "model_config", {}) or {}
    base_args = config.get("base_args")
    if base_args is None:
        print("model command error: missing startup model configuration")
        return None

    provider = updates.get("provider", config.get("provider", getattr(base_args, "provider", "openai")))
    if provider not in MODEL_PROVIDERS:
        print(f"model command error: unsupported provider: {provider}")
        print(MODEL_USAGE)
        return None

    model = updates.get("model")
    if model is None and "provider" not in updates:
        model = config.get("model")
    elif model is None and provider == "deepseek":
        model = DEFAULT_DEEPSEEK_MODEL_FOR_MODEL_COMMAND

    next_args = _copy_args_with_model(base_args, provider, model)
    next_client = _build_model_client(next_args)
    resolved_model = _effective_model(next_args, provider)
    agent.model_client = next_client
    agent.model_config = {
        "provider": provider,
        "model": resolved_model,
        "base_args": base_args,
    }
    print(f"model updated: provider={provider} model={resolved_model}")
    return None


def _handle_slash_command(user_input, agent):
    for name, _desc, handler in _COMMANDS:
        if user_input == name:
            result = handler(agent)
            if result is _ACTION_EXIT:
                return _ACTION_EXIT
            return True
    if user_input.startswith("/model "):
        _handle_model_command(agent, user_input)
        return True
    return False


# 创建欢迎文本
def build_welcome(agent, model, host):
    workspace = getattr(agent.workspace, "repo_root", "-")
    branch = getattr(agent.workspace, "branch", "-")
    approval = getattr(agent, "approval_policy", "-")
    session = agent.session.get("id", "-")

    return "\n".join(
        [
            "+==================================================================================+",
            "|                           Mini Coding Agent                                      |",
            "|                                local coding agent                                |",
            "|                            calm shell, ready for work                            |",
            "+----------------------------------------------------------------------------------+",
            "|                                                                                  |",
            f"| WORKSPACE  {workspace:<64} |",
            f"| MODEL      {model:<30} BRANCH    {branch:<30} |",
            f"| APPROVAL   {approval:<30} SESSION   {session:<30} |",
            "|                                                                                  |",
            "+==================================================================================+",
        ]
    )


# 根据参数创建Agent实例
# 实例包含模型对象、当前工作区、Session存储、secret配置、最大步数和最大token数等
def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 Mca 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Mca`，或一个从旧 session 恢复出来的 `Mca`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照和加载项目级环境，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    load_project_env(workspace.repo_root)
    configured_secret_names = _configured_secret_names(args)
    store = SessionStore(workspace.repo_root + "/.mca/sessions")
    model = _build_model_client(args)
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    if session_id:
        agent = Mca.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            enable_mcp=getattr(args, "enable_mcp", False),
        )
        return _apply_agent_mode(_attach_model_config(agent, args), args)
    agent = Mca(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        enable_mcp=getattr(args, "enable_mcp", False),
    )
    return _apply_agent_mode(_attach_model_config(agent, args), args)


# 负责参数解析
def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for Ollama, OpenAI-compatible, Anthropic-compatible, or DeepSeek models.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--provider", choices=("ollama", "openai", "anthropic", "deepseek"), default="openai", help="Model backend to use.")
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override. Defaults to qwen3.5:4b for Ollama, MCA_OPENAI_MODEL for openai, MCA_ANTHROPIC_MODEL for anthropic, and MCA_DEEPSEEK_MODEL for deepseek when set.",
    )
    parser.add_argument("--host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for openai, anthropic, or deepseek.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--resume", default=None, help="Session id to resume or 'latest'.")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument("--max-steps", type=int, default=6, help="Maximum tool/model iterations per request.")
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature sent to Ollama.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
    parser.add_argument("--enable-mcp", dest="enable_mcp", action="store_true", default=False, help="Enable MCP server discovery.")
    parser.add_argument("--disable-mcp", dest="enable_mcp", action="store_false", help="Disable MCP server discovery.")
    return parser


def build_web_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Run the local Mca web console.",
    )
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument("--host", default="127.0.0.1", help="Web bind host.")
    parser.add_argument("--port", type=int, default=8765, help="Web bind port.")
    parser.add_argument("--provider", choices=("ollama", "openai", "anthropic", "deepseek"), default="deepseek", help="Model backend to use.")
    parser.add_argument("--model", default=None, help="Model name override.")
    parser.add_argument("--base-url", default=None, help="Provider API base URL for openai, anthropic, or deepseek.")
    parser.add_argument("--ollama-host", default=DEFAULT_OLLAMA_HOST, help="Ollama server URL.")
    parser.add_argument("--ollama-timeout", type=int, default=300, help="Ollama request timeout in seconds.")
    parser.add_argument("--openai-timeout", type=int, default=300, help="OpenAI-compatible request timeout in seconds.")
    parser.add_argument("--approval", choices=("ask", "auto", "never"), default="ask", help="Approval policy for risky tools.")
    parser.add_argument("--max-steps", type=int, default=6, help="Maximum tool/model iterations per request.")
    parser.add_argument("--max-new-tokens", type=int, default=512, help="Maximum model output tokens per step.")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature.")
    parser.add_argument("--top-p", type=float, default=0.9, help="Top-p sampling value sent to Ollama.")
    parser.add_argument("--disable-mcp", dest="enable_mcp", action="store_false", default=True, help="Disable MCP server discovery.")
    return parser


class StreamPrinter:
    """流式输出处理器：负责标签过滤和终端写入。"""

    def __init__(self, write_func):
        self.write = write_func
        self.buffer = ""
        self.printed = 0
        self.in_final = False

    def reset(self):
        """重置状态，用于 ask() 内多次模型调用间清零。"""
        self.buffer = ""
        self.printed = 0
        self.in_final = False

    def __call__(self, chunk, state):
        if state == "tool":
            return
        self.buffer += chunk
        if not self.in_final:
            if "<tool" in self.buffer and ("<final>" not in self.buffer or self.buffer.find("<tool") < self.buffer.find("<final>")):
                return
            if "<final>" not in self.buffer:
                return
            # 如果包含 < 但 <final> 未完整出现，等待下一个 chunk
            if "<" in self.buffer and "<final>" not in self.buffer:
                last_lt = self.buffer.rfind("<")
                if last_lt != -1 and not self.buffer[last_lt:].startswith("</"):
                    return
            self.in_final = True
        text = self.buffer
        if "<final>" in text:
            text = text[text.find("<final>") + len("<final>"):]
        repeated_final = text.find("<final")
        if repeated_final != -1:
            text = text[:repeated_final]
        if "</final>" in text:
            text = text[:text.find("</final>")]
        else:
            # 保护不完整的 </final> 跨 chunk 分割
            last_close = text.rfind("</")
            if last_close != -1:
                text = text[:last_close]
            # 保护不完整的 <final> 开标签跨 chunk 分割，避免重复完整文本
            # 或异常 provider 事件把控制标签残片刷到终端。
            for prefix_length in range(len("<final>") - 1, 0, -1):
                if text.endswith("<final>"[:prefix_length]):
                    text = text[:-prefix_length]
                    break
        new_text = text[self.printed:]
        if new_text:
            self.write(new_text)
            self.printed += len(new_text)


def _make_session_writer(session):
    def write(text):
        sys.stdout.write(text)
        sys.stdout.flush()

    return write


def _make_stdout_writer():
    def write(text):
        sys.stdout.write(text)
        sys.stdout.flush()

    return write


def _build_bottom_toolbar(agent):
    """构建固定在输入栏下方的上下文预算状态栏。"""

    def toolbar():
        meta = getattr(agent, "last_prompt_metadata", None) or {}
        if not meta:
            mode = getattr(agent, "mode", "ReAct")
            budget = getattr(agent.context_manager, "total_budget", 12000)
            return HTML(f"<style fg='ansigreen'>ctx: 0/{budget} (0%) | ready | mode={mode}</style>")

        prompt_chars = meta.get("prompt_chars", 0)
        budget = meta.get("prompt_budget_chars", 12000)
        pct = (prompt_chars / budget * 100) if budget > 0 else 0
        reduced = bool(meta.get("budget_reductions"))

        if meta.get("prompt_over_budget") or pct >= 90:
            color = "ansired"
        elif pct >= 70:
            color = "ansiyellow"
        else:
            color = "ansigreen"

        sections = meta.get("sections", {})
        history_chars = sections.get("history", {}).get("rendered_chars", 0)
        memory_chars = sections.get("memory", {}).get("rendered_chars", 0)
        prefix_chars = sections.get("prefix", {}).get("rendered_chars", 0)

        reduced_flag = " <style fg='ansired'>[reduced]</style>" if reduced else ""

        return HTML(
            f"<style fg='{color}'>ctx: {prompt_chars}/{budget} ({pct:.0f}%)</style>"
            f" | history:{history_chars} memory:{memory_chars} prefix:{prefix_chars}"
            f" | mode:{getattr(agent, 'mode', 'ReAct')}"
            f"{reduced_flag}"
        )

    return toolbar


# 执行入口
def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "web":
        web_args = build_web_arg_parser().parse_args(argv[1:])
        from .web import run_web

        return run_web(web_args)

    args = build_arg_parser().parse_args(argv)
    agent = build_agent(args)

    model = getattr(agent.model_client, "model", getattr(args, "model", DEFAULT_OLLAMA_MODEL))
    host = getattr(agent.model_client, "host", getattr(agent.model_client, "base_url", getattr(args, "host", DEFAULT_OLLAMA_HOST)))
    print(build_welcome(agent, model=model, host=host))

    if args.prompt:
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                printer = StreamPrinter(_make_stdout_writer())
                answer = agent.ask(prompt, stream=True, on_chunk=printer)
                if answer:
                    print(answer)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    # Agent循环
    session = PromptSession(
        message="\nmca> ",
        completer=_SlashCommandCompleter(),
        complete_while_typing=True,
        bottom_toolbar=_build_bottom_toolbar(agent),
    )

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        try:
            user_input = session.prompt().strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        if user_input == "/":
            print(HELP_DETAILS)
            continue

        command_result = _handle_slash_command(user_input, agent)
        if command_result is _ACTION_EXIT:
            return 0
        if command_result:
            continue

        print()
        try:
            printer = StreamPrinter(_make_session_writer(session))
            answer = agent.ask(user_input, stream=True, on_chunk=printer)
            if printer.printed > 0:
                sys.stdout.write("\n")
            elif answer:
                print(answer)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
