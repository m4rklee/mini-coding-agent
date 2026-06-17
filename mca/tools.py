"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import asyncio
import json
import re
import shutil
import subprocess
import threading
import textwrap
from functools import partial
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from .workspace import IGNORED_PATH_NAMES, clip

SKILLS = {}
CAPABILITY_CONFIG_RELATIVE_PATH = ".mca/config/capabilities.json"
MCP_CONFIG_RELATIVE_PATH = ".mca/config/mcp_servers.json"
MCP_SERVER_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
SKILL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

DEFAULT_MCP_SERVERS = [
    {
        "name": "math",
        "command": "uv",
        "args": ["run", "python", "examples/mcp_math_server.py"],
        "env": {},
        "enabled": True,
        "builtin": True,
    },
    {
        "name": "notes",
        "command": "uv",
        "args": ["run", "python", "examples/mcp_notes_server.py"],
        "env": {},
        "enabled": True,
        "builtin": True,
    },
]

BASE_TOOL_SPECS = {
    "list_files": {
        "schema": {"path": "str='.'"},
        "risky": False,
        "description": "List files in the workspace.",
    },
    "read_file": {
        "schema": {"path": "str", "start": "int=1", "end": "int=200"},
        "risky": False,
        "description": "Read a UTF-8 file by line range.",
    },
    "search": {
        "schema": {"pattern": "str", "path": "str='.'"},
        "risky": False,
        "description": "Search the workspace with rg or a simple fallback.",
    },
    "run_shell": {
        "schema": {"command": "str", "timeout": "int=20"},
        "risky": True,
        "description": "Run a shell command in the repo root.",
    },
    "write_file": {
        "schema": {"path": "str", "content": "str"},
        "risky": True,
        "description": "Write a text file.",
    },
    "patch_file": {
        "schema": {"path": "str", "old_text": "str", "new_text": "str"},
        "risky": True,
        "description": "Replace one exact text block in a file.",
    },
    "load_skill":{
        "schema": {"name": "str"},
        "risky": False,
        "description": "Load a skill from local directory"
    }
}

DELEGATE_TOOL_SPEC = {
    "schema": {"task": "str", "max_steps": "int=3"},
    "risky": False,
    "description": "Ask a bounded read-only child agent to investigate.",
}

TOOL_EXAMPLES = {
    "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
    "search": '<tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>',
    "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
    "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
    "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
    "delegate": '<tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>',
    "load_skill": '<tool>{"name:"load_skill","args":{"name":"example.skill"}}</tool>'
}


def capability_config_path(root):
    return Path(root) / CAPABILITY_CONFIG_RELATIVE_PATH


def load_capability_config(root):
    path = capability_config_path(root)
    if not path.exists():
        return {"tools": {}, "skills": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid capabilities config JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("Capabilities config must be a JSON object")
    tools = raw.get("tools", {}) or {}
    skills = raw.get("skills", {}) or {}
    if not isinstance(tools, dict) or not isinstance(skills, dict):
        raise ValueError("Capabilities config fields 'tools' and 'skills' must be objects")
    return {
        "tools": {str(name): bool(enabled) for name, enabled in tools.items()},
        "skills": {str(name): bool(enabled) for name, enabled in skills.items()},
    }


def write_capability_config(root, config):
    path = capability_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = {
        "tools": {str(name): bool(enabled) for name, enabled in (config.get("tools", {}) or {}).items()},
        "skills": {str(name): bool(enabled) for name, enabled in (config.get("skills", {}) or {}).items()},
    }
    path.write_text(json.dumps(normalized, indent=2, ensure_ascii=False), encoding="utf-8")
    return normalized


def is_tool_enabled(root, name):
    return load_capability_config(root).get("tools", {}).get(name, True)


def is_skill_enabled(root, name):
    return load_capability_config(root).get("skills", {}).get(name, True)


def _known_tool_specs():
    return {**BASE_TOOL_SPECS, "delegate": DELEGATE_TOOL_SPEC}


def set_tool_enabled(root, name, enabled):
    if name not in _known_tool_specs():
        raise KeyError(name)
    config = load_capability_config(root)
    config.setdefault("tools", {})[name] = bool(enabled)
    return write_capability_config(root, config)


def set_skill_enabled(root, name, enabled):
    if not SKILL_NAME_RE.match(name):
        raise ValueError("Invalid skill name")
    skill_path = Path(root) / "skills" / name / "SKILL.md"
    if not skill_path.exists():
        raise KeyError(name)
    config = load_capability_config(root)
    config.setdefault("skills", {})[name] = bool(enabled)
    return write_capability_config(root, config)


def list_public_tool_specs(root):
    return [
        {
            "name": name,
            "schema": spec.get("schema", {}),
            "risky": bool(spec.get("risky")),
            "description": spec.get("description", ""),
            "enabled": is_tool_enabled(root, name),
            "builtin": True,
        }
        for name, spec in _known_tool_specs().items()
    ]


# 工具注册器
def build_tool_registry(agent):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    # 
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], agent)}
        for name, spec in BASE_TOOL_SPECS.items()
        if is_tool_enabled(agent.root, name)
    }
    # 子 agent 是刻意做成受限能力的：一旦深度耗尽，
    # 就连 delegate 这个工具都不再暴露给模型。
    if agent.depth < agent.max_depth and is_tool_enabled(agent.root, "delegate"):
        tools["delegate"] = {**DELEGATE_TOOL_SPEC, "run": partial(tool_delegate, agent)}
    
    # MCP 工具注册
    agent.mcp_clients = {}
    if not getattr(agent, "enable_mcp", False):
        return tools
    mcp_servers = load_mcp_server_configs(agent.root)
    for server in mcp_servers:
        if not server.get("enabled", True):
            continue
        client = MCPClient(
            name=server['name'],
            command=server['command'],
            args=server.get('args', []),
            env=server.get("env") or None,
        )
        try:
            client.connect_sync()
        except Exception:
            try:
                client.close_sync()
            except Exception:
                pass
            continue
        agent.mcp_clients[server['name']] = client

        for mcp_tool in client.list_tools_sync():
            mcp_tool_name = f"mcp.{server['name']}.{mcp_tool.name}"
            
            tools[mcp_tool_name] = {
                "schema": {
                    "_json_schema": mcp_tool.inputSchema,
                },
                "risky": True,
                "description": mcp_tool.description or "",
                "run": partial(
                    _run_mcp_tool,
                    client,
                    mcp_tool.name,
                )
            }

    return tools


def mcp_config_path(root):
    return Path(root) / MCP_CONFIG_RELATIVE_PATH


def _normalize_mcp_server(raw, *, builtin=False):
    if not isinstance(raw, dict):
        raise ValueError("MCP server entry must be an object")
    name = str(raw.get("name", "")).strip()
    if not MCP_SERVER_NAME_RE.match(name):
        raise ValueError(f"invalid MCP server name: {name}")
    command = str(raw.get("command", "")).strip()
    if not command:
        raise ValueError(f"MCP server {name} command must not be empty")
    args = raw.get("args", [])
    if args is None:
        args = []
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValueError(f"MCP server {name} args must be a list of strings")
    env = raw.get("env", {})
    if env is None:
        env = {}
    if not isinstance(env, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in env.items()):
        raise ValueError(f"MCP server {name} env must be an object of string values")
    return {
        "name": name,
        "command": command,
        "args": list(args),
        "env": dict(env),
        "enabled": bool(raw.get("enabled", True)),
        "builtin": bool(raw.get("builtin", builtin)),
    }


def validate_mcp_config_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("MCP config must be a JSON object")
    servers = payload.get("servers")
    if not isinstance(servers, list):
        raise ValueError("MCP config must include a servers list")
    normalized = []
    seen = set()
    for raw in servers:
        server = _normalize_mcp_server(raw)
        if server["name"] in seen:
            raise ValueError(f"duplicate MCP server name: {server['name']}")
        seen.add(server["name"])
        normalized.append(server)
    return normalized


def load_user_mcp_server_configs(root):
    path = mcp_config_path(root)
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    return validate_mcp_config_payload(payload)


def write_user_mcp_server_configs(root, servers):
    normalized = validate_mcp_config_payload({"servers": servers})
    path = mcp_config_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": normalized}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return normalized


def load_mcp_server_configs(root):
    merged = {}
    for server in DEFAULT_MCP_SERVERS:
        normalized = _normalize_mcp_server(server, builtin=True)
        merged[normalized["name"]] = normalized
    for server in load_user_mcp_server_configs(root):
        merged[server["name"]] = server
    return list(merged.values())


def public_mcp_server_configs(root):
    public = []
    for server in load_mcp_server_configs(root):
        item = dict(server)
        if item.get("env"):
            item["env"] = {key: "<redacted>" for key in item["env"]}
        public.append(item)
    return public


def set_mcp_server_enabled(root, name, enabled):
    merged = {server["name"]: server for server in load_mcp_server_configs(root)}
    if name not in merged:
        raise KeyError(name)
    user_servers = {server["name"]: server for server in load_user_mcp_server_configs(root)}
    server = dict(user_servers.get(name, merged[name]))
    server["enabled"] = bool(enabled)
    user_servers[name] = server
    write_user_mcp_server_configs(root, list(user_servers.values()))
    return server


def tool_example(name):
    return TOOL_EXAMPLES.get(name, "")

# 工具校验
'''
    1.工具调用路径是否逃逸
    2.工具参数是否合理
'''
def validate_tool(agent, name, args):
    args = args or {}

    if name == "list_files":
        path = agent.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        agent.path(args.get("path", "."))
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        return

    if name == "write_file":
        path = agent.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = agent.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return

    if name == "delegate":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        return

    if name == "load_skill":
        skill_name = str(args.get("name", "")).strip()
        if not skill_name:
            raise ValueError("skill name must not be empty")
        if skill_name not in SKILLS:
            raise ValueError(f"unknown skill: {skill_name}")
        return 


def tool_list_files(agent, args):
    path = agent.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(agent.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return f"# {path.relative_to(agent.root)}\n{body}"


def tool_search(agent, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = agent.path(args.get("path", "."))

    if shutil.which("rg"):
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        result = subprocess.run(
            ["rg", "-n", "--smart-case", "--max-count", "200", pattern, str(path)],
            cwd=agent.root,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no matches)"

    matches = []
    files = [path] if path.is_file() else [
        item for item in path.rglob("*")
        if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(agent.root).parts)
    ]
    for file_path in files:
        for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if pattern.lower() in line.lower():
                matches.append(f"{file_path.relative_to(agent.root)}:{number}:{line}")
                if len(matches) >= 200:
                    return "\n".join(matches)
    return "\n".join(matches) or "(no matches)"


def tool_run_shell(agent, args):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 20))
    if timeout < 1 or timeout > 120:
        raise ValueError("timeout must be in [1, 120]")
    result = subprocess.run(
        command,
        cwd=agent.root,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        # 这里传入的是过滤后的环境变量，而不是直接继承整个父 shell 环境，
        # 目的是减少敏感信息被意外带进命令执行环境的风险。
        env=agent.shell_env(),
    )
    return textwrap.dedent(
        f"""\
        exit_code: {result.returncode}
        stdout:
        {result.stdout.strip() or "(empty)"}
        stderr:
        {result.stderr.strip() or "(empty)"}
        """
    ).strip()


def tool_write_file(agent, args):
    path = agent.path(args["path"])
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {path.relative_to(agent.root)} ({len(content)} chars)"


def tool_patch_file(agent, args):
    path = agent.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count != 1:
        raise ValueError(f"old_text must occur exactly once, found {count}")
    path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
    return f"patched {path.relative_to(agent.root)}"


def tool_delegate(agent, args):
    if agent.depth >= agent.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")

    from .runtime import Mca

    child = Mca(
        model_client=agent.model_client,
        workspace=agent.workspace,
        session_store=agent.session_store,
        run_store=agent.run_store,
        approval_policy="never",
        max_steps=int(args.get("max_steps", 3)),
        max_new_tokens=agent.max_new_tokens,
        depth=agent.depth + 1,
        max_depth=agent.max_depth,
        read_only=True,
        secret_env_names=agent.secret_env_names,
        shell_env_allowlist=agent.shell_env_allowlist,
        enable_mcp=getattr(agent, "enable_mcp", False),
    )
    # 委派的目标是“调查”，不是“放权执行”。
    # 子 agent 以只读方式运行、步数更少，最后只把结论文本返回给父 agent。
    child.session["memory"]["task"] = task
    child.session["memory"]["notes"] = [clip(agent.history_text(), 300)]
    return "delegate_result:\n" + child.ask(task)

def tool_load_skill(agent, args):
    # 根据skill名称读取skill
    name = args["name"]
    if name not in getattr(agent, "skills", {}) or name not in SKILLS:
        raise ValueError(f"skill is disabled or not found: {name}")
    # 加载skill内容到agent
    content = SKILLS[name]['instruction']
    return "load_skill: \n" + content

_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search": tool_search,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "load_skill": tool_load_skill
}

# skill注册
# SKILLS = {}
def build_skill_registry(agent):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    #
    SKILLS.clear()
    available_skills = {}
    for skill in discover_skill_specs(agent.root, include_instruction=True):
        if not skill.get("enabled", True):
            continue
        skill_name = skill["name"]
        SKILLS[skill_name] = {
            "metadata": {"description": skill.get("description", "")},
            "instruction": skill.get("instruction", ""),
        }
        available_skills[skill_name] = {"description": skill.get("description", ""), "enabled": True}
    return available_skills

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
def parse_skill(skill_content):
    metadata = {}

    match = FRONTMATTER_RE.match(skill_content)
    if match:
        metadata = yaml.safe_load(match.group(1)) or {}
        body = skill_content[match.end():]
    else:
        body = skill_content

    return metadata, body


def discover_skill_specs(root, include_instruction=False):
    skills_dir = Path(root) / "skills"
    if not skills_dir.is_dir():
        return []
    specs = []
    for skill_dir in sorted(skills_dir.iterdir(), key=lambda item: item.name.lower()):
        if not skill_dir.is_dir() or not SKILL_NAME_RE.match(skill_dir.name):
            continue
        skill_path = skill_dir / "SKILL.md"
        if not skill_path.is_file():
            continue
        with open(skill_path, "r", encoding="utf-8") as f:
            skill_text = f.read()
        metadata, instruction = parse_skill(skill_text)
        spec = {
            "name": skill_dir.name,
            "description": metadata.get("description", ""),
            "enabled": is_skill_enabled(root, skill_dir.name),
        }
        if include_instruction:
            spec["instruction"] = instruction
        specs.append(spec)
    return specs


# MCP相关
def content_to_text(result) -> str:
    parts = []

    for block in result.content:
        if isinstance(block, types.TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{block.type} content]")
    return '\n'.join(parts)

# MCP客户端，用于发现和调用
class MCPClient:
    def __init__(self, name, command, args=None, env=None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env
        self.session = None
        self._stdio_context = None
        self._session_context = None
        self._closed_event = None
        self._connection_future = None
        self._ready_event = threading.Event()
        self._connect_error = None
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            name=f"mcp-{name}",
            daemon=True,
        )
        self._loop_thread.start()

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _run_sync(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    def connect_sync(self):
        self._connection_future = asyncio.run_coroutine_threadsafe(
            self._connection_main(),
            self._loop,
        )
        self._ready_event.wait()
        if self._connect_error:
            raise self._connect_error

    def list_tools_sync(self):
        return self._run_sync(self.list_tools())

    def call_tool_sync(self, tool_name, args):
        return self._run_sync(self.call_tool(tool_name, args))

    def close_sync(self):
        try:
            if self._closed_event:
                self._loop.call_soon_threadsafe(self._closed_event.set)
            if self._connection_future:
                self._connection_future.result(timeout=5)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=2)

    async def _connection_main(self):
        server_params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env
        )
        try:
            async with stdio_client(server_params) as (read, write):
                async with ClientSession(read, write) as session:
                    self.session = session
                    await self.session.initialize()
                    self._closed_event = asyncio.Event()
                    self._ready_event.set()
                    await self._closed_event.wait()
        except Exception as exc:
            self._connect_error = exc
            self._ready_event.set()
            raise
        finally:
            self.session = None
    
    async def list_tools(self):
        result = await self.session.list_tools()
        return result.tools
    
    async def call_tool(self, tool_name, args):
        result = await self.session.call_tool(tool_name, arguments=args)
        return content_to_text(result)

def _run_mcp_tool(client, tool_name, args):
    return client.call_tool_sync(tool_name, args)
