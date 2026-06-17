import argparse
import json

from fastapi.testclient import TestClient

import mca.web as web_module
from mca.models import FakeModelClient
from mca.runtime import Mca, SessionStore
from mca.web import create_app
from mca.workspace import WorkspaceContext


def web_args(tmp_path, **overrides):
    values = {
        "cwd": str(tmp_path),
        "host": "127.0.0.1",
        "port": 8765,
        "provider": "ollama",
        "model": None,
        "base_url": None,
        "ollama_host": "http://127.0.0.1:11434",
        "ollama_timeout": 1,
        "openai_timeout": 1,
        "approval": "ask",
        "max_steps": 2,
        "max_new_tokens": 64,
        "temperature": 0.2,
        "top_p": 0.9,
        "enable_mcp": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_web_api_creates_and_lists_sessions(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    app = create_app(web_args(tmp_path))

    with TestClient(app) as client:
        created = client.post("/api/sessions").json()
        assert created["id"]

        listed = client.get("/api/sessions").json()
        assert listed["sessions"][0]["id"] == created["id"]

        detail = client.get(f"/api/sessions/{created['id']}").json()
        assert detail["history"] == []


def test_web_import_skill_validates_and_reports_conflict(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    app = create_app(web_args(tmp_path))
    skill_text = "---\nname: hello_web\ndescription: Web greeting skill.\n---\n\nSay hello.\n"

    with TestClient(app) as client:
        response = client.post(
            "/api/imports/skills",
            files={"file": ("SKILL.md", skill_text, "text/markdown")},
        )
        assert response.status_code == 200
        assert (tmp_path / "skills" / "hello_web" / "SKILL.md").exists()

        conflict = client.post(
            "/api/imports/skills",
            files={"file": ("SKILL.md", skill_text, "text/markdown")},
        )
        assert conflict.status_code == 409

        overwrite = client.post(
            "/api/imports/skills?confirmOverwrite=true",
            files={"file": ("SKILL.md", skill_text, "text/markdown")},
        )
        assert overwrite.status_code == 200


def test_web_import_mcp_config_conflicts_with_builtin_until_confirmed(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    app = create_app(web_args(tmp_path))
    payload = {
        "servers": [
            {
                "name": "notes",
                "command": "uv",
                "args": ["run", "python", "examples/mcp_notes_server.py"],
                "env": {"TOKEN": "secret"},
                "enabled": True,
            }
        ]
    }

    with TestClient(app) as client:
        conflict = client.post("/api/imports/mcp", json=payload)
        assert conflict.status_code == 409

        saved = client.post("/api/imports/mcp?confirmOverwrite=true", json=payload)
        assert saved.status_code == 200

        public = client.get("/api/capabilities/mcp").json()
        notes = next(server for server in public["servers"] if server["name"] == "notes")
        assert notes["env"]["TOKEN"] == "<redacted>"


def test_web_config_reports_and_switches_mode(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    app = create_app(web_args(tmp_path))

    with TestClient(app) as client:
        config = client.get("/api/config").json()
        assert config["cwd"] == str(tmp_path.resolve())
        assert config["mode"] == "ReAct"

        response = client.patch("/api/config", json={"mode": "plan"})
        assert response.status_code == 200
        assert response.json()["mode"] == "plan"

        created = client.post("/api/sessions").json()
        registry = app.state.registry
        assert registry.agents[created["id"]].mode == "plan"


def test_web_config_switches_workspace_and_refreshes_store(tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "README.md").write_text("a\n", encoding="utf-8")
    (root_b / "README.md").write_text("b\n", encoding="utf-8")
    app = create_app(web_args(root_a))

    with TestClient(app) as client:
        created = client.post("/api/sessions").json()
        assert (root_a / ".mca" / "sessions" / f"{created['id']}.json").exists()

        response = client.patch("/api/config", json={"cwd": str(root_b), "mode": "plan"})
        assert response.status_code == 200
        config = response.json()
        assert config["cwd"] == str(root_b.resolve())
        assert config["mode"] == "plan"
        assert app.state.registry.agents == {}

        assert client.get("/api/sessions").json()["sessions"] == []
        created_b = client.post("/api/sessions").json()
        assert (root_b / ".mca" / "sessions" / f"{created_b['id']}.json").exists()


def test_web_config_rejects_missing_workspace(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    app = create_app(web_args(tmp_path))

    with TestClient(app) as client:
        response = client.patch("/api/config", json={"cwd": str(tmp_path / "missing")})
        assert response.status_code == 404


def test_web_directory_picker_returns_selected_path(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    selected = tmp_path / "selected"
    selected.mkdir()
    captured = []

    def fake_picker(initial=None):
        captured.append(initial)
        return str(selected)

    monkeypatch.setattr(web_module, "pick_directory", fake_picker)
    app = create_app(web_args(tmp_path))

    with TestClient(app) as client:
        response = client.post("/api/dialog/directory", json={"initial": str(tmp_path)})
        assert response.status_code == 200
        assert response.json() == {"path": str(selected)}
        assert captured == [str(tmp_path)]


def test_web_directory_picker_reports_unavailable(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")

    def fake_picker(initial=None):
        raise RuntimeError("picker unavailable")

    monkeypatch.setattr(web_module, "pick_directory", fake_picker)
    app = create_app(web_args(tmp_path))

    with TestClient(app) as client:
        response = client.post("/api/dialog/directory", json={"initial": str(tmp_path)})
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "DIRECTORY_PICKER_UNAVAILABLE"


def test_web_can_disable_builtin_tool_and_clear_active_agents(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    app = create_app(web_args(tmp_path))

    with TestClient(app) as client:
        tools = client.get("/api/capabilities/tools").json()["tools"]
        run_shell = next(tool for tool in tools if tool["name"] == "run_shell")
        assert run_shell["enabled"] is True

        created = client.post("/api/sessions").json()
        assert created["id"] in app.state.registry.agents

        response = client.patch("/api/capabilities/tools/run_shell", json={"enabled": False})
        assert response.status_code == 200
        assert response.json()["refreshed"]["closedAgents"] == 1
        assert app.state.registry.agents == {}

        config = json.loads((tmp_path / ".mca" / "config" / "capabilities.json").read_text(encoding="utf-8"))
        assert config["tools"]["run_shell"] is False

        tools = client.get("/api/capabilities/tools").json()["tools"]
        run_shell = next(tool for tool in tools if tool["name"] == "run_shell")
        assert run_shell["enabled"] is False

        restored = app.state.registry.get(created["id"], lambda *_args: None)
        assert "run_shell" not in restored.tools


def test_web_can_disable_skill_and_keep_it_visible(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    skill_dir = tmp_path / "skills" / "hello"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: hello\ndescription: Say hello.\n---\n\nHello instruction.\n", encoding="utf-8")
    app = create_app(web_args(tmp_path))

    with TestClient(app) as client:
        skills = client.get("/api/capabilities/skills").json()["skills"]
        assert next(skill for skill in skills if skill["name"] == "hello")["enabled"] is True

        response = client.patch("/api/capabilities/skills/hello", json={"enabled": False})
        assert response.status_code == 200

        skills = client.get("/api/capabilities/skills").json()["skills"]
        assert next(skill for skill in skills if skill["name"] == "hello")["enabled"] is False

        created = client.post("/api/sessions").json()
        agent = app.state.registry.agents[created["id"]]
        assert "hello" not in agent.skills


def test_web_can_disable_builtin_mcp_server(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    app = create_app(web_args(tmp_path))

    with TestClient(app) as client:
        response = client.patch("/api/capabilities/mcp/notes", json={"enabled": False})
        assert response.status_code == 200

        servers = client.get("/api/capabilities/mcp").json()["servers"]
        assert next(server for server in servers if server["name"] == "notes")["enabled"] is False

        payload = json.loads((tmp_path / ".mca" / "config" / "mcp_servers.json").read_text(encoding="utf-8"))
        notes = next(server for server in payload["servers"] if server["name"] == "notes")
        assert notes["enabled"] is False


def test_web_capability_change_rejects_while_run_in_progress(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    app = create_app(web_args(tmp_path))

    with TestClient(app) as client:
        created = client.post("/api/sessions").json()
        lock = app.state.registry.lock_for(created["id"])
        lock.acquire()
        try:
            response = client.patch("/api/capabilities/tools/read_file", json={"enabled": False})
            assert response.status_code == 409
        finally:
            lock.release()


def test_web_metrics_aggregates_run_reports(tmp_path):
    run_dir = tmp_path / ".mca" / "runs" / "run_1"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(
        json.dumps(
            {
                "run_id": "run_1",
                "session_id": "session_1",
                "status": "completed",
                "stop_reason": "final_answer_returned",
                "tool_steps": 2,
                "attempts": 3,
                "prompt_metadata": {
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "total_tokens": 15,
                    "cached_tokens": 4,
                    "cache_hit": True,
                },
            }
        ),
        encoding="utf-8",
    )
    app = create_app(web_args(tmp_path))

    with TestClient(app) as client:
        metrics = client.get("/api/metrics/runs").json()
        assert metrics["totalRuns"] == 1
        assert metrics["statusCounts"] == {"completed": 1}
        assert metrics["inputTokens"] == 10
        assert metrics["cacheHitRate"] == 1


def test_mca_close_closes_mcp_clients(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".mca" / "sessions")
    agent = Mca(FakeModelClient(["<final>ok</final>"]), workspace, store)
    closed = []

    class FakeMcpClient:
        def close_sync(self):
            closed.append(True)

    agent.mcp_clients = {"fake": FakeMcpClient()}
    agent.close()
    assert closed == [True]
    assert agent.mcp_clients == {}
