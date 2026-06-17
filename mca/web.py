"""Local Web console for Mca."""

from __future__ import annotations

import argparse
import asyncio
import json
import platform
import queue
import subprocess
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MethodType

import uvicorn
from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .cli import build_agent
from .runtime import Mca, SessionStore
from .workspace import clip
from . import tools as toolkit

SKILL_NAME_RE = toolkit.MCP_SERVER_NAME_RE
SSE_DONE = object()


@dataclass
class ApprovalRequest:
    approval_id: str
    event: threading.Event
    approved: bool | None = None


class ApprovalBroker:
    def __init__(self, emit):
        self.emit = emit
        self._pending: dict[str, ApprovalRequest] = {}
        self._lock = threading.Lock()

    def request(self, agent, name, args):
        approval_id = "approval_" + uuid.uuid4().hex[:10]
        item = ApprovalRequest(approval_id=approval_id, event=threading.Event())
        with self._lock:
            self._pending[approval_id] = item
        self.emit(
            "approval_required",
            {
                "approvalId": approval_id,
                "toolName": name,
                "args": agent.redact_artifact(args),
            },
        )
        item.event.wait(timeout=300)
        with self._lock:
            self._pending.pop(approval_id, None)
        return bool(item.approved)

    def resolve(self, approval_id, approved):
        with self._lock:
            item = self._pending.get(approval_id)
        if item is None:
            raise KeyError(approval_id)
        item.approved = bool(approved)
        item.event.set()


class StreamPrinter:
    def __init__(self, emit):
        self.emit = emit
        self.buffer = ""
        self.printed = 0
        self.in_final = False

    def reset(self):
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
            self.in_final = True
        text = self.buffer
        if "<final>" in text:
            text = text[text.find("<final>") + len("<final>"):]
        if "</final>" in text:
            text = text[:text.find("</final>")]
        new_text = text[self.printed:]
        if new_text:
            self.emit("assistant_delta", {"text": new_text})
            self.printed += len(new_text)


class WebAgentRegistry:
    VALID_MODES = {"ReAct", "plan"}

    def __init__(self, startup_args):
        self.startup_args = startup_args
        self.root = Path(startup_args.cwd).resolve()
        self.mode = getattr(startup_args, "mode", "ReAct") or "ReAct"
        self.store = SessionStore(self.root / ".mca" / "sessions")
        self.agents: dict[str, Mca] = {}
        self.locks: dict[str, threading.Lock] = {}
        self._approval_brokers: dict[str, ApprovalBroker] = {}
        self._lock = threading.Lock()

    def _agent_args(self, resume=None):
        return argparse.Namespace(
            cwd=str(self.root),
            provider=self.startup_args.provider,
            model=self.startup_args.model,
            base_url=self.startup_args.base_url,
            host=self.startup_args.ollama_host,
            ollama_timeout=self.startup_args.ollama_timeout,
            openai_timeout=self.startup_args.openai_timeout,
            temperature=self.startup_args.temperature,
            top_p=self.startup_args.top_p,
            resume=resume,
            approval=self.startup_args.approval,
            secret_env_names=[],
            max_steps=self.startup_args.max_steps,
            max_new_tokens=self.startup_args.max_new_tokens,
            enable_mcp=self.startup_args.enable_mcp,
            mode=self.mode,
        )

    def _wire_agent(self, agent, emit):
        broker = ApprovalBroker(emit)
        agent.event_sink = emit
        agent.approve = MethodType(lambda this, name, args: broker.request(this, name, args), agent)
        self._approval_brokers[agent.session["id"]] = broker
        return agent

    def create(self):
        q = queue.Queue()

        def emit(event, payload=None):
            q.put({"event": event, "payload": payload or {}})

        agent = self._wire_agent(build_agent(self._agent_args()), emit)
        session_id = agent.session["id"]
        with self._lock:
            self.agents[session_id] = agent
            self.locks[session_id] = threading.Lock()
        return agent

    def get(self, session_id, emit):
        with self._lock:
            agent = self.agents.get(session_id)
        if agent is None:
            agent = build_agent(self._agent_args(resume=session_id))
            with self._lock:
                self.agents[session_id] = agent
                self.locks.setdefault(session_id, threading.Lock())
        return self._wire_agent(agent, emit)

    def lock_for(self, session_id):
        with self._lock:
            return self.locks.setdefault(session_id, threading.Lock())

    def config(self):
        return {
            "cwd": str(self.root),
            "mode": self.mode,
            "provider": self.startup_args.provider,
            "model": getattr(self.startup_args, "model", None) or "",
            "approval": self.startup_args.approval,
        }

    def set_mode(self, mode):
        if mode not in self.VALID_MODES:
            raise ValueError("mode must be ReAct or plan")
        with self._lock:
            self.mode = mode
            agents = list(self.agents.values())
        for agent in agents:
            agent.mode = mode
            agent.refresh_prefix(force=True)
        return self.config()

    def switch_workspace(self, cwd, mode=None):
        path = Path(cwd).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise FileNotFoundError(str(path))
        if mode is not None and mode not in self.VALID_MODES:
            raise ValueError("mode must be ReAct or plan")
        with self._lock:
            if any(lock.locked() for lock in self.locks.values()):
                raise RuntimeError("Cannot switch workspace while a run is in progress")
            agents = list(self.agents.values())
            self.agents.clear()
            self.locks.clear()
            self._approval_brokers.clear()
            self.root = path
            self.store = SessionStore(self.root / ".mca" / "sessions")
            if mode is not None:
                self.mode = mode
        for agent in agents:
            try:
                agent.close()
            except Exception:
                pass
        return self.config()

    def reset_agents_for_capability_change(self):
        with self._lock:
            if any(lock.locked() for lock in self.locks.values()):
                raise RuntimeError("Cannot update capabilities while a run is in progress")
            agents = list(self.agents.values())
            self.agents.clear()
            self.locks.clear()
            self._approval_brokers.clear()
        for agent in agents:
            try:
                agent.close()
            except Exception:
                pass
        return {"strategy": "clear_active_agents", "closedAgents": len(agents)}

    def resolve_approval(self, approval_id, approved):
        for broker in list(self._approval_brokers.values()):
            try:
                broker.resolve(approval_id, approved)
                return
            except KeyError:
                continue
        raise KeyError(approval_id)

    def close(self):
        for agent in list(self.agents.values()):
            agent.close()

    def forget(self, session_id):
        """Drop in-memory state for a session. Caller is responsible for the file."""
        with self._lock:
            agent = self.agents.pop(session_id, None)
            self.locks.pop(session_id, None)
            self._approval_brokers.pop(session_id, None)
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass

    def inspect_agent(self):
        agent = build_agent(self._agent_args())
        path = Path(agent.session_path)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        return agent


def error_response(status_code, code, message, details=None):
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "details": details or {}}},
    )


def session_summary(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    history = list(data.get("history", []))
    last = history[-1] if history else {}
    runtime = data.get("runtime_identity", {}) or {}
    # Use mtime as the "last activity" timestamp for sorting/display in the UI.
    try:
        updated_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        updated_at = ""
    return {
        "id": data.get("id", path.stem),
        "title": data.get("title", ""),
        "createdAt": data.get("created_at", ""),
        "updatedAt": updated_at,
        "messageCount": len(history),
        "lastMessage": clip(last.get("content", ""), 160) if last else "",
        "lastRole": last.get("role", "") if last else "",
        "model": runtime.get("model", ""),
        "provider": runtime.get("model_client", ""),
    }


def load_runs(root, session_id=None):
    rows = []
    runs_root = Path(root) / ".mca" / "runs"
    for report_path in sorted(runs_root.glob("*/report.json"), key=lambda item: item.stat().st_mtime if item.exists() else 0, reverse=True):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        report_session_id = str(report.get("session_id") or "unassigned")
        if session_id and report_session_id != session_id:
            continue
        prompt = report.get("prompt_metadata", {}) or {}
        rows.append(
            {
                "runId": report.get("run_id", report_path.parent.name),
                "sessionId": report_session_id,
                "status": report.get("status", ""),
                "stopReason": report.get("stop_reason", ""),
                "toolSteps": int(report.get("tool_steps", 0) or 0),
                "attempts": int(report.get("attempts", 0) or 0),
                "inputTokens": int(prompt.get("input_tokens", 0) or 0),
                "outputTokens": int(prompt.get("output_tokens", 0) or 0),
                "totalTokens": int(prompt.get("total_tokens", 0) or 0),
                "cachedTokens": int(prompt.get("cached_tokens", 0) or 0),
                "cacheHit": bool(prompt.get("cache_hit")),
            }
        )
    return rows


def aggregate_runs(rows):
    total = len(rows)
    statuses = {}
    for row in rows:
        status = row.get("status") or "unknown"
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "totalRuns": total,
        "statusCounts": statuses,
        "avgToolSteps": sum(row["toolSteps"] for row in rows) / total if total else 0,
        "avgAttempts": sum(row["attempts"] for row in rows) / total if total else 0,
        "inputTokens": sum(row["inputTokens"] for row in rows),
        "outputTokens": sum(row["outputTokens"] for row in rows),
        "totalTokens": sum(row["totalTokens"] for row in rows),
        "cachedTokens": sum(row["cachedTokens"] for row in rows),
        "cacheHitRate": sum(1 for row in rows if row["cacheHit"]) / total if total else 0,
        "rows": rows,
    }


def _escape_applescript_text(value):
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _pick_directory_with_osascript(initial=None):
    script = 'POSIX path of (choose folder with prompt "选择 mca 工作目录"'
    initial_path = Path(initial).expanduser() if initial else None
    if initial_path and initial_path.exists() and initial_path.is_dir():
        escaped = _escape_applescript_text(str(initial_path.resolve()))
        script += f' default location POSIX file "{escaped}"'
    script += ")"
    completed = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # 用户取消时 osascript 通常返回非 0，并在 stderr 里写 User canceled。
    if completed.returncode != 0:
        if "User canceled" in completed.stderr:
            return ""
        raise RuntimeError(completed.stderr.strip() or "osascript directory picker failed")
    return completed.stdout.strip()


def _pick_directory_with_tk(initial=None):
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        raise RuntimeError("tkinter is not available") from exc
    root = tk.Tk()
    root.withdraw()
    try:
        return filedialog.askdirectory(
            initialdir=str(Path(initial).expanduser()) if initial else None,
            title="选择 mca 工作目录",
        ) or ""
    finally:
        root.destroy()


def pick_directory(initial=None):
    if platform.system() == "Darwin":
        try:
            return _pick_directory_with_osascript(initial)
        except RuntimeError:
            # 继续尝试 tkinter 兜底。
            pass
    return _pick_directory_with_tk(initial)


def encode_sse(event, payload):
    return f"event: {event}\ndata: {json.dumps(payload or {}, ensure_ascii=False)}\n\n"


def create_app(startup_args):
    registry = WebAgentRegistry(startup_args)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            registry.close()

    app = FastAPI(title="Mca Web Console", lifespan=lifespan)
    app.state.registry = registry

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request, exc):
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
        return error_response(exc.status_code, detail.get("code", "HTTP_ERROR"), detail.get("message", str(exc.detail)), detail.get("details", {}))

    @app.get("/api/config")
    def get_config():
        return registry.config()

    @app.patch("/api/config")
    async def patch_config(request: Request):
        body = await request.json()
        cwd = str(body.get("cwd", "")).strip() if "cwd" in body else None
        mode = str(body.get("mode", "")).strip() if "mode" in body else None
        if mode == "Agent":
            mode = "ReAct"
        if mode == "Plan":
            mode = "plan"
        if mode == "":
            mode = None
        try:
            if cwd:
                return registry.switch_workspace(cwd, mode=mode)
            if mode is not None:
                return registry.set_mode(mode)
            return registry.config()
        except FileNotFoundError:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "Workspace directory not found"}) from None
        except RuntimeError as exc:
            raise HTTPException(409, {"code": "RUN_IN_PROGRESS", "message": str(exc)}) from None
        except ValueError as exc:
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": str(exc)}) from None

    @app.post("/api/dialog/directory")
    async def choose_directory(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        initial = str(body.get("initial", "")).strip() if isinstance(body, dict) else ""
        try:
            return {"path": pick_directory(initial or str(registry.root))}
        except RuntimeError as exc:
            raise HTTPException(501, {"code": "DIRECTORY_PICKER_UNAVAILABLE", "message": str(exc)}) from None

    @app.post("/api/sessions")
    def create_session():
        agent = registry.create()
        return {"id": agent.session["id"], "createdAt": agent.session.get("created_at", "")}

    @app.get("/api/sessions")
    def list_sessions():
        rows = []
        for path in sorted((registry.root / ".mca" / "sessions").glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            summary = session_summary(path)
            if summary:
                rows.append(summary)
        return {"sessions": rows}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        path = registry.store.path(session_id)
        if not path.exists():
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "Session not found"})
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "id": data.get("id", session_id),
            "createdAt": data.get("created_at", ""),
            "history": data.get("history", []),
            "memory": data.get("memory", {}),
            "checkpoints": data.get("checkpoints", {}),
            "runs": load_runs(registry.root, session_id=session_id),
        }

    @app.patch("/api/sessions/{session_id}")
    async def patch_session(session_id: str, request: Request):
        path = registry.store.path(session_id)
        if not path.exists():
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "Session not found"})
        body = await request.json()
        if "title" not in body:
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": "title is required"})
        title = str(body.get("title", "")).strip()[:120]
        # Update on disk first; if it was loaded into memory, refresh that copy too
        # so subsequent ask() calls don't overwrite our title with stale state.
        data = json.loads(path.read_text(encoding="utf-8"))
        data["title"] = title
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        with registry._lock:
            agent = registry.agents.get(session_id)
        if agent is not None:
            try:
                agent.session["title"] = title
            except Exception:
                pass
        return {"id": session_id, "title": title}

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str):
        path = registry.store.path(session_id)
        if not path.exists():
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "Session not found"})
        # Refuse if a run is currently active on this session.
        lock = registry.lock_for(session_id)
        if not lock.acquire(blocking=False):
            raise HTTPException(409, {"code": "RUN_IN_PROGRESS", "message": "Cannot delete: a run is in progress"})
        try:
            registry.forget(session_id)
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        finally:
            lock.release()
        return {"id": session_id, "deleted": True}

    @app.post("/api/sessions/{session_id}/messages/stream")
    async def stream_message(session_id: str, request: Request):
        body = await request.json()
        message = str(body.get("message", "")).strip()
        if not message:
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": "message must not be empty"})

        event_queue: queue.Queue = queue.Queue()

        def emit(event, payload=None):
            event_queue.put({"event": event, "payload": payload or {}})

        try:
            agent = registry.get(session_id, emit)
        except FileNotFoundError:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "Session not found"}) from None

        lock = registry.lock_for(session_id)
        if not lock.acquire(blocking=False):
            raise HTTPException(409, {"code": "RUN_IN_PROGRESS", "message": "A run is already active for this session"})

        def worker():
            try:
                printer = StreamPrinter(emit)
                answer = agent.ask(message, stream=True, on_chunk=printer)
                if answer:
                    emit("assistant_delta", {"text": answer})
                emit("final", {"answer": answer or agent.current_task_state.final_answer if agent.current_task_state else ""})
            except Exception as exc:
                emit("error", {"message": str(exc)})
            finally:
                lock.release()
                event_queue.put(SSE_DONE)

        threading.Thread(target=worker, name=f"mca-web-{session_id}", daemon=True).start()

        async def event_stream():
            while True:
                item = await asyncio.to_thread(event_queue.get)
                if item is SSE_DONE:
                    break
                yield encode_sse(item["event"], item["payload"])

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.post("/api/approvals/{approval_id}")
    async def resolve_approval(approval_id: str, request: Request):
        body = await request.json()
        try:
            registry.resolve_approval(approval_id, bool(body.get("approved")))
        except KeyError:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "Approval request not found"}) from None
        return {"ok": True}

    def _read_enabled(body):
        if not isinstance(body, dict) or "enabled" not in body or not isinstance(body.get("enabled"), bool):
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": "enabled must be a boolean"})
        return bool(body["enabled"])

    def _reset_capability_agents():
        try:
            return registry.reset_agents_for_capability_change()
        except RuntimeError as exc:
            raise HTTPException(409, {"code": "RUN_IN_PROGRESS", "message": str(exc)}) from None

    @app.get("/api/capabilities/tools")
    def list_tools():
        return {"tools": sorted(toolkit.list_public_tool_specs(registry.root), key=lambda item: item["name"])}

    @app.patch("/api/capabilities/tools/{name}")
    async def set_tool_enabled(name: str, request: Request):
        enabled = _read_enabled(await request.json())
        try:
            toolkit.set_tool_enabled(registry.root, name, enabled)
        except KeyError:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "Tool not found"}) from None
        except ValueError as exc:
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": str(exc)}) from None
        return {"name": name, "enabled": enabled, "refreshed": _reset_capability_agents()}

    @app.get("/api/capabilities/skills")
    def list_skills():
        return {"skills": sorted(toolkit.discover_skill_specs(registry.root), key=lambda item: item["name"])}

    @app.patch("/api/capabilities/skills/{name}")
    async def set_skill_enabled(name: str, request: Request):
        enabled = _read_enabled(await request.json())
        try:
            toolkit.set_skill_enabled(registry.root, name, enabled)
        except KeyError:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "Skill not found"}) from None
        except ValueError as exc:
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": str(exc)}) from None
        return {"name": name, "enabled": enabled, "refreshed": _reset_capability_agents()}

    @app.get("/api/capabilities/mcp")
    def list_mcp():
        servers = toolkit.public_mcp_server_configs(registry.root)
        for server in servers:
            server["tools"] = []
        if startup_args.enable_mcp:
            agent = registry.inspect_agent()
            try:
                tools_by_server = {server["name"]: [] for server in servers}
                for name, spec in sorted(agent.tools.items()):
                    parts = name.split(".", 2)
                    if len(parts) != 3 or parts[0] != "mcp":
                        continue
                    tools_by_server.setdefault(parts[1], []).append(
                        {
                            "name": name,
                            "schema": spec.get("schema", {}),
                            "risky": bool(spec.get("risky")),
                            "description": spec.get("description", ""),
                        }
                    )
                for server in servers:
                    server["tools"] = tools_by_server.get(server["name"], [])
            finally:
                agent.close()
        return {"servers": servers}

    @app.patch("/api/capabilities/mcp/{name}")
    async def set_mcp_enabled(name: str, request: Request):
        enabled = _read_enabled(await request.json())
        try:
            toolkit.set_mcp_server_enabled(registry.root, name, enabled)
        except KeyError:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "MCP server not found"}) from None
        except ValueError as exc:
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": str(exc)}) from None
        return {"name": name, "enabled": enabled, "refreshed": _reset_capability_agents()}

    @app.post("/api/imports/skills")
    async def import_skill(file: UploadFile = File(...), confirmOverwrite: bool = Query(False)):
        raw = (await file.read()).decode("utf-8")
        metadata, _instruction = toolkit.parse_skill(raw)
        name = str(metadata.get("name") or Path(file.filename or "").stem).strip()
        if not SKILL_NAME_RE.match(name):
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": "Skill frontmatter must include a valid name"})
        if not str(metadata.get("description", "")).strip():
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": "Skill frontmatter must include a description"})
        path = registry.root / "skills" / name / "SKILL.md"
        if path.exists() and not confirmOverwrite:
            raise HTTPException(409, {"code": "CONFLICT", "message": f"Skill already exists: {name}"})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(raw, encoding="utf-8")
        return {"name": name, "path": str(path.relative_to(registry.root))}

    @app.post("/api/imports/mcp")
    async def import_mcp(request: Request, confirmOverwrite: bool = Query(False)):
        payload = await request.json()
        incoming = toolkit.validate_mcp_config_payload(payload)
        existing = {server["name"]: server for server in toolkit.load_mcp_server_configs(registry.root)}
        user_existing = {server["name"]: server for server in toolkit.load_user_mcp_server_configs(registry.root)}
        conflicts = [server["name"] for server in incoming if server["name"] in existing]
        if conflicts and not confirmOverwrite:
            raise HTTPException(409, {"code": "CONFLICT", "message": "MCP server already exists", "details": {"names": conflicts}})
        user_existing.update({server["name"]: server for server in incoming})
        saved = toolkit.write_user_mcp_server_configs(registry.root, list(user_existing.values()))
        return {"servers": [{"name": server["name"], "enabled": server["enabled"]} for server in saved]}

    @app.get("/api/metrics/runs")
    def metrics_runs():
        return aggregate_runs(load_runs(registry.root))

    dist = Path(__file__).resolve().parent.parent / "web" / "dist"
    if dist.exists():
        assets = dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/{path:path}")
        def serve_frontend(path: str):
            candidate = dist / path
            if path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app


def run_web(args):
    args.cwd = str(Path(args.cwd).resolve())
    app = create_app(args)
    print(f"mca web console: http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port)
    return 0
