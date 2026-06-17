import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Activity,
  Bot,
  Check,
  ChevronDown,
  Database,
  FileUp,
  Folder,
  MessageSquarePlus,
  PanelLeftClose,
  PanelLeftOpen,
  Pencil,
  Plug,
  RefreshCw,
  Search,
  Send,
  ShieldAlert,
  Sparkles,
  TerminalSquare,
  Trash2,
  Wrench,
  X
} from "lucide-react";
import "./styles.css";

type Tab = "chat" | "capabilities" | "metrics" | "imports";
type AgentMode = "ReAct" | "plan";

type AppConfig = {
  cwd: string;
  mode: AgentMode;
  provider: string;
  model: string;
  approval: string;
};

type SessionSummary = {
  id: string;
  title?: string;
  createdAt: string;
  updatedAt?: string;
  messageCount: number;
  lastMessage: string;
  lastRole: string;
  model: string;
  provider: string;
};

type HistoryItem = {
  role: string;
  content: string;
  name?: string;
  args?: Record<string, unknown>;
  created_at?: string;
};

type ToolSpec = {
  name: string;
  schema: Record<string, unknown>;
  risky: boolean;
  description: string;
  enabled: boolean;
};

type SkillSpec = {
  name: string;
  description: string;
  enabled: boolean;
};

type McpServer = {
  name: string;
  command: string;
  args: string[];
  enabled: boolean;
  builtin?: boolean;
};

type Metrics = {
  totalRuns: number;
  statusCounts: Record<string, number>;
  avgToolSteps: number;
  avgAttempts: number;
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  cachedTokens: number;
  cacheHitRate: number;
};

type ChatEvent =
  | { type: "user"; text: string }
  | { type: "assistant"; text: string }
  | { type: "tool"; text: string; status?: string }
  | { type: "system"; text: string; status?: string };

type Approval = {
  approvalId: string;
  toolName: string;
  args: Record<string, unknown>;
};

const ROLE_LABEL: Record<string, string> = {
  user: "用户",
  assistant: "助手",
  tool: "工具",
  system: "系统"
};

function roleLabel(role: string): string {
  return ROLE_LABEL[role] ?? role;
}

const TOOL_DESCRIPTION_ZH: Record<string, string> = {
  list_files: "列出工作区里的文件。",
  read_file: "按行号区间读取一个 UTF-8 文本文件。",
  search: "用 rg（或简易回退）在工作区里搜索。",
  run_shell: "在仓库根目录执行一条 shell 命令。",
  write_file: "写入一个文本文件。",
  patch_file: "替换文件里某一段精确匹配的文本。",
  load_skill: "从本地目录加载一个 skill。",
  delegate: "派出一个受限、只读的子 agent 去做调研。"
};

function toolDescription(tool: ToolSpec): string {
  return TOOL_DESCRIPTION_ZH[tool.name] ?? tool.description;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: init?.body instanceof FormData ? undefined : { "Content-Type": "application/json" },
    ...init
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload?.error?.message || `请求失败：${response.status}`);
  }
  return response.json() as Promise<T>;
}

function fmtNumber(value: number) {
  return new Intl.NumberFormat().format(Math.round(value || 0));
}

function basename(path: string): string {
  const clean = path.replace(/\/+$/, "");
  return clean.split("/").filter(Boolean).pop() || clean || "/";
}

function fmtRelativeTime(iso?: string): string {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "";
  const diffSec = Math.round((Date.now() - t) / 1000);
  if (diffSec < 30) return "刚刚";
  if (diffSec < 60) return `${diffSec} 秒前`;
  if (diffSec < 3600) return `${Math.round(diffSec / 60)} 分钟前`;
  const date = new Date(t);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) {
    return `今天 ${date.getHours().toString().padStart(2, "0")}:${date.getMinutes().toString().padStart(2, "0")}`;
  }
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "昨天";
  if (date.getFullYear() === today.getFullYear()) return `${date.getMonth() + 1}月${date.getDate()}日`;
  return `${date.getFullYear()}-${(date.getMonth() + 1).toString().padStart(2, "0")}-${date.getDate().toString().padStart(2, "0")}`;
}

type TimeBucket = "today" | "yesterday" | "week" | "older";
const BUCKET_ORDER: TimeBucket[] = ["today", "yesterday", "week", "older"];
const BUCKET_LABEL: Record<TimeBucket, string> = {
  today: "今天",
  yesterday: "昨天",
  week: "最近 7 天",
  older: "更早"
};

function bucketOf(iso?: string): TimeBucket {
  if (!iso) return "older";
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return "older";
  const date = new Date(t);
  const today = new Date();
  if (date.toDateString() === today.toDateString()) return "today";
  const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
  if (date.toDateString() === yesterday.toDateString()) return "yesterday";
  const weekAgo = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 7);
  if (date >= weekAgo) return "week";
  return "older";
}

function sessionDisplayTitle(session: SessionSummary): string {
  if (session.title && session.title.trim()) return session.title.trim();
  if (session.lastMessage && session.lastMessage.trim()) return session.lastMessage.trim().slice(0, 40);
  return session.id;
}

function App() {
  const [tab, setTab] = useState<Tab>("chat");
  const [config, setConfig] = useState<AppConfig | null>(null);
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeSessionId, setActiveSessionId] = useState("");
  const [tools, setTools] = useState<ToolSpec[]>([]);
  const [skills, setSkills] = useState<SkillSpec[]>([]);
  const [mcp, setMcp] = useState<McpServer[]>([]);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [chatEvents, setChatEvents] = useState<ChatEvent[]>([]);
  const [message, setMessage] = useState("");
  const [isRunning, setIsRunning] = useState(false);
  const [approval, setApproval] = useState<Approval | null>(null);
  const [notice, setNotice] = useState("");
  const [sessionPanelCollapsed, setSessionPanelCollapsed] = useState<boolean>(() => {
    try {
      return localStorage.getItem("mca.sessionPanelCollapsed") === "1";
    } catch {
      return false;
    }
  });

  useEffect(() => {
    try {
      localStorage.setItem("mca.sessionPanelCollapsed", sessionPanelCollapsed ? "1" : "0");
    } catch {
      // localStorage 不可用就算了。
    }
  }, [sessionPanelCollapsed]);

  async function refreshConfig() {
    setConfig(await api<AppConfig>("/api/config"));
  }

  async function updateConfig(patch: Partial<Pick<AppConfig, "cwd" | "mode">>) {
    const next = await api<AppConfig>("/api/config", { method: "PATCH", body: JSON.stringify(patch) });
    setConfig(next);
    setActiveSessionId("");
    setChatEvents([]);
    await Promise.all([refreshSessions(), refreshCapabilities(), refreshMetrics()]);
  }

  async function refreshSessions(selectLatest = false) {
    const payload = await api<{ sessions: SessionSummary[] }>("/api/sessions");
    setSessions(payload.sessions);
    if (selectLatest && payload.sessions[0] && !activeSessionId) {
      setActiveSessionId(payload.sessions[0].id);
    }
  }

  async function createSession() {
    const payload = await api<{ id: string }>("/api/sessions", { method: "POST" });
    setActiveSessionId(payload.id);
    setChatEvents([]);
    await refreshSessions();
  }

  async function loadSession(id: string) {
    if (!id) return;
    const payload = await api<{ history: HistoryItem[] }>(`/api/sessions/${id}`);
    setChatEvents(
      payload.history.map((item) => ({
        type: item.role === "tool" ? "tool" : item.role === "user" ? "user" : item.role === "system" ? "system" : "assistant",
        text: item.role === "tool" ? `${item.name}: ${item.content}` : item.content
      }))
    );
  }

  async function renameSession(id: string, title: string) {
    await api(`/api/sessions/${id}`, { method: "PATCH", body: JSON.stringify({ title }) });
    await refreshSessions();
  }

  async function deleteSession(id: string) {
    await api(`/api/sessions/${id}`, { method: "DELETE" });
    if (activeSessionId === id) {
      setActiveSessionId("");
      setChatEvents([]);
    }
    await refreshSessions();
  }

  async function refreshCapabilities() {
    const [toolPayload, skillPayload, mcpPayload] = await Promise.all([
      api<{ tools: ToolSpec[] }>("/api/capabilities/tools"),
      api<{ skills: SkillSpec[] }>("/api/capabilities/skills"),
      api<{ servers: McpServer[] }>("/api/capabilities/mcp")
    ]);
    setTools(toolPayload.tools);
    setSkills(skillPayload.skills);
    setMcp(mcpPayload.servers);
  }

  async function toggleCapability(kind: "tools" | "skills" | "mcp", name: string, enabled: boolean) {
    try {
      await api(`/api/capabilities/${kind}/${encodeURIComponent(name)}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled })
      });
      await refreshCapabilities();
      setNotice(enabled ? `已启用：${name}` : `已禁用：${name}`);
    } catch (error) {
      setNotice((error as Error).message);
    }
  }

  async function refreshMetrics() {
    setMetrics(await api<Metrics>("/api/metrics/runs"));
  }

  useEffect(() => {
    refreshConfig().catch((error) => setNotice(error.message));
    refreshSessions(true).catch((error) => setNotice(error.message));
    refreshCapabilities().catch((error) => setNotice(error.message));
    refreshMetrics().catch((error) => setNotice(error.message));
  }, []);

  useEffect(() => {
    if (activeSessionId) {
      loadSession(activeSessionId).catch((error) => setNotice(error.message));
    }
  }, [activeSessionId]);

  async function sendMessage() {
    const text = message.trim();
    if (!text || isRunning) return;
    let sessionId = activeSessionId;
    if (!sessionId) {
      const created = await api<{ id: string }>("/api/sessions", { method: "POST" });
      sessionId = created.id;
      setActiveSessionId(sessionId);
    }
    setMessage("");
    setIsRunning(true);
    setChatEvents((items) => [...items, { type: "user", text }, { type: "assistant", text: "" }]);
    try {
      const response = await fetch(`/api/sessions/${sessionId}/messages/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text })
      });
      if (!response.ok || !response.body) {
        throw new Error(`流式响应失败：${response.status}`);
      }
      await readEventStream(response.body);
      await Promise.all([refreshSessions(), refreshMetrics(), loadSession(sessionId)]);
    } catch (error) {
      setChatEvents((items) => [...items, { type: "system", status: "error", text: (error as Error).message }]);
    } finally {
      setIsRunning(false);
    }
  }

  async function readEventStream(body: ReadableStream<Uint8Array>) {
    const reader = body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        handleSseBlock(block);
        boundary = buffer.indexOf("\n\n");
      }
    }
  }

  function handleSseBlock(block: string) {
    const event = block.split("\n").find((line) => line.startsWith("event:"))?.slice(6).trim() || "message";
    const data = block.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trim()).join("\n");
    const payload = data ? JSON.parse(data) : {};
    if (event === "assistant_delta") {
      setChatEvents((items) => {
        const next = [...items];
        const last = next[next.length - 1];
        if (last?.type === "assistant") {
          next[next.length - 1] = { ...last, text: last.text + (payload.text || "") };
        } else {
          next.push({ type: "assistant", text: payload.text || "" });
        }
        return next;
      });
    } else if (event === "tool_started") {
      setChatEvents((items) => [...items, { type: "tool", status: "running", text: `正在执行 ${payload.name}` }]);
    } else if (event === "tool_finished") {
      setChatEvents((items) => [...items, { type: "tool", status: payload.tool_status || "ok", text: `${payload.name}: ${payload.result || ""}` }]);
    } else if (event === "approval_required") {
      setApproval(payload as Approval);
    } else if (event === "error") {
      setChatEvents((items) => [...items, { type: "system", status: "error", text: payload.message || "未知错误" }]);
    }
  }

  async function resolveApproval(approved: boolean) {
    if (!approval) return;
    await api(`/api/approvals/${approval.approvalId}`, {
      method: "POST",
      body: JSON.stringify({ approved })
    });
    setApproval(null);
  }

  const tabs = useMemo(
    () => [
      { id: "chat" as Tab, label: "聊天", icon: Bot },
      { id: "capabilities" as Tab, label: "能力", icon: Wrench },
      { id: "metrics" as Tab, label: "指标", icon: Activity },
      { id: "imports" as Tab, label: "导入", icon: FileUp }
    ],
    []
  );

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <TerminalSquare size={26} />
          <div>
            <strong>mca 控制台</strong>
            <span>本地 agent 工作区</span>
          </div>
        </div>
        <nav className="nav">
          {tabs.map((item) => {
            const Icon = item.icon;
            return (
              <button className={tab === item.id ? "active" : ""} key={item.id} onClick={() => setTab(item.id)} title={item.label}>
                <Icon size={18} />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
      </aside>

      <section className="content">
        <header className="topbar">
          <div>
            <h1>{tabs.find((item) => item.id === tab)?.label}</h1>
            <p>{tab === "chat" ? (activeSessionId || "暂无活动会话") : ""}</p>
          </div>
          <button className="icon-button" onClick={() => Promise.all([refreshConfig(), refreshSessions(), refreshCapabilities(), refreshMetrics()])} title="刷新">
            <RefreshCw size={18} />
          </button>
        </header>
        {notice && (
          <div className="notice">
            <span>{notice}</span>
            <button onClick={() => setNotice("")} title="关闭">
              <X size={16} />
            </button>
          </div>
        )}
        {tab === "chat" && (
          <ChatTab
            sessions={sessions}
            activeSessionId={activeSessionId}
            setActiveSessionId={setActiveSessionId}
            createSession={createSession}
            renameSession={renameSession}
            deleteSession={deleteSession}
            collapsed={sessionPanelCollapsed}
            onToggleCollapsed={() => setSessionPanelCollapsed((v) => !v)}
            config={config}
            updateConfig={updateConfig}
            setNotice={setNotice}
            events={chatEvents}
            message={message}
            setMessage={setMessage}
            sendMessage={sendMessage}
            isRunning={isRunning}
          />
        )}
        {tab === "capabilities" && <CapabilitiesView tools={tools} skills={skills} mcp={mcp} onToggle={toggleCapability} />}
        {tab === "metrics" && <MetricsView metrics={metrics} />}
        {tab === "imports" && <ImportsView onDone={() => Promise.all([refreshCapabilities(), refreshSessions()])} />}
      </section>

      {approval && (
        <div className="modal-backdrop">
          <div className="modal">
            <ShieldAlert size={26} />
            <h2>审批工具调用</h2>
            <p>{approval.toolName}</p>
            <pre>{JSON.stringify(approval.args, null, 2)}</pre>
            <div className="modal-actions">
              <button onClick={() => resolveApproval(false)}>
                <X size={16} /> 拒绝
              </button>
              <button className="primary" onClick={() => resolveApproval(true)}>
                <Check size={16} /> 批准
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

function ChatTab(props: {
  sessions: SessionSummary[];
  activeSessionId: string;
  setActiveSessionId: (id: string) => void;
  createSession: () => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  config: AppConfig | null;
  updateConfig: (patch: Partial<Pick<AppConfig, "cwd" | "mode">>) => Promise<void>;
  setNotice: (message: string) => void;
  events: ChatEvent[];
  message: string;
  setMessage: (value: string) => void;
  sendMessage: () => void;
  isRunning: boolean;
}) {
  return (
    <div className={`chat-tab ${props.collapsed ? "session-collapsed" : ""}`}>
      <SessionListPanel
        sessions={props.sessions}
        activeSessionId={props.activeSessionId}
        setActiveSessionId={props.setActiveSessionId}
        createSession={props.createSession}
        renameSession={props.renameSession}
        deleteSession={props.deleteSession}
        collapsed={props.collapsed}
        onToggleCollapsed={props.onToggleCollapsed}
      />
      <ChatView
        config={props.config}
        updateConfig={props.updateConfig}
        setNotice={props.setNotice}
        events={props.events}
        message={props.message}
        setMessage={props.setMessage}
        sendMessage={props.sendMessage}
        isRunning={props.isRunning}
      />
    </div>
  );
}

function SessionListPanel(props: {
  sessions: SessionSummary[];
  activeSessionId: string;
  setActiveSessionId: (id: string) => void;
  createSession: () => Promise<void>;
  renameSession: (id: string, title: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  collapsed: boolean;
  onToggleCollapsed: () => void;
}) {
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState<Record<TimeBucket, boolean>>({
    today: false,
    yesterday: false,
    week: false,
    older: true
  });
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return props.sessions;
    return props.sessions.filter((session) => {
      const title = sessionDisplayTitle(session).toLowerCase();
      return title.includes(q) || session.id.toLowerCase().includes(q) || (session.lastMessage || "").toLowerCase().includes(q);
    });
  }, [props.sessions, query]);

  const grouped = useMemo(() => {
    const buckets: Record<TimeBucket, SessionSummary[]> = { today: [], yesterday: [], week: [], older: [] };
    for (const session of filtered) buckets[bucketOf(session.updatedAt || session.createdAt)].push(session);
    return buckets;
  }, [filtered]);

  function startRename(session: SessionSummary) {
    setEditingId(session.id);
    setEditingValue(session.title || sessionDisplayTitle(session));
  }

  async function commitRename() {
    if (!editingId) return;
    const id = editingId;
    const value = editingValue.trim();
    setEditingId(null);
    setEditingValue("");
    try {
      await props.renameSession(id, value);
    } catch (error) {
      console.error(error);
    }
  }

  if (props.collapsed) {
    return (
      <aside className="session-panel collapsed">
        <button className="icon-button rail-button" onClick={props.onToggleCollapsed} title="展开会话列表">
          <PanelLeftOpen size={18} />
        </button>
        <button className="icon-button rail-button primary" onClick={() => props.createSession()} title="新建会话">
          <MessageSquarePlus size={18} />
        </button>
      </aside>
    );
  }

  return (
    <aside className="session-panel">
      <div className="session-panel-header">
        <div className="session-panel-top-row">
          <button className="primary new-session" onClick={() => props.createSession()}>
            <MessageSquarePlus size={16} /> 新建会话
          </button>
          <button className="icon-button" onClick={props.onToggleCollapsed} title="收起会话列表">
            <PanelLeftClose size={16} />
          </button>
        </div>
        <div className="session-search">
          <Search size={14} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索标题或内容…" />
          {query && (
            <button className="icon-button small" onClick={() => setQuery("")} title="清空搜索">
              <X size={12} />
            </button>
          )}
        </div>
      </div>

      <div className="session-list">
        {filtered.length === 0 && <div className="empty small">{query ? "没有匹配的会话。" : "还没有会话。点上面的按钮新建一个。"}</div>}
        {BUCKET_ORDER.map((bucket) => {
          const items = grouped[bucket];
          if (items.length === 0) return null;
          const isCollapsed = collapsed[bucket];
          return (
            <div className="session-bucket" key={bucket}>
              <button className="session-bucket-header" onClick={() => setCollapsed((prev) => ({ ...prev, [bucket]: !prev[bucket] }))}>
                <span className={`caret ${isCollapsed ? "collapsed" : ""}`}>▾</span>
                <span>{BUCKET_LABEL[bucket]}</span>
                <span className="bucket-count">{items.length}</span>
              </button>
              {!isCollapsed &&
                items.map((session) => {
                  const isActive = props.activeSessionId === session.id;
                  const isEditing = editingId === session.id;
                  return (
                    <div
                      key={session.id}
                      className={`session-row ${isActive ? "active" : ""}`}
                      onClick={() => !isEditing && props.setActiveSessionId(session.id)}
                      onDoubleClick={(event) => {
                        event.stopPropagation();
                        startRename(session);
                      }}
                    >
                      <div className="session-row-main">
                        {isEditing ? (
                          <RenameInput
                            value={editingValue}
                            onChange={setEditingValue}
                            onCommit={commitRename}
                            onCancel={() => {
                              setEditingId(null);
                              setEditingValue("");
                            }}
                          />
                        ) : (
                          <strong className="session-row-title">{sessionDisplayTitle(session)}</strong>
                        )}
                        <span className="session-row-time">{fmtRelativeTime(session.updatedAt || session.createdAt)}</span>
                      </div>
                      {!isEditing && session.lastMessage && <p className="session-row-preview">{session.lastMessage}</p>}
                      {!isEditing && (
                        <div className="session-row-actions">
                          <button
                            className="icon-button small"
                            title="重命名"
                            onClick={(event) => {
                              event.stopPropagation();
                              startRename(session);
                            }}
                          >
                            <Pencil size={12} />
                          </button>
                          <button
                            className="icon-button small danger"
                            title="删除会话"
                            onClick={(event) => {
                              event.stopPropagation();
                              setConfirmDeleteId(session.id);
                            }}
                          >
                            <Trash2 size={12} />
                          </button>
                        </div>
                      )}
                    </div>
                  );
                })}
            </div>
          );
        })}
      </div>

      {confirmDeleteId && (
        <div className="modal-backdrop" onClick={() => setConfirmDeleteId(null)}>
          <div className="modal" onClick={(event) => event.stopPropagation()}>
            <Trash2 size={26} />
            <h2>删除会话？</h2>
            <p>会话 <code>{confirmDeleteId}</code> 将被永久删除（包括磁盘上的会话文件）。此操作不可撤销。</p>
            <div className="modal-actions">
              <button onClick={() => setConfirmDeleteId(null)}>
                <X size={16} /> 取消
              </button>
              <button
                className="primary danger"
                onClick={async () => {
                  const id = confirmDeleteId;
                  setConfirmDeleteId(null);
                  await props.deleteSession(id);
                }}
              >
                <Trash2 size={16} /> 删除
              </button>
            </div>
          </div>
        </div>
      )}
    </aside>
  );
}

function RenameInput(props: { value: string; onChange: (value: string) => void; onCommit: () => void; onCancel: () => void }) {
  const ref = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);
  return (
    <input
      ref={ref}
      className="rename-input"
      value={props.value}
      onChange={(event) => props.onChange(event.target.value)}
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event) => {
        if (event.key === "Enter") {
          event.preventDefault();
          props.onCommit();
        } else if (event.key === "Escape") {
          event.preventDefault();
          props.onCancel();
        }
      }}
      onBlur={() => props.onCommit()}
      placeholder="给这个会话起个标题"
    />
  );
}

function ChatView(props: {
  config: AppConfig | null;
  updateConfig: (patch: Partial<Pick<AppConfig, "cwd" | "mode">>) => Promise<void>;
  setNotice: (message: string) => void;
  events: ChatEvent[];
  message: string;
  setMessage: (value: string) => void;
  sendMessage: () => void;
  isRunning: boolean;
}) {
  const hasMessages = props.events.length > 0;
  return (
    <div className={`chat-layout ${hasMessages ? "has-messages" : "empty-chat"}`}>
      {!hasMessages && (
        <section className="agent-hero">
          <h2>我们应该在这个仓库里做些什么？</h2>
          <PromptCard
            config={props.config}
            updateConfig={props.updateConfig}
            setNotice={props.setNotice}
            message={props.message}
            setMessage={props.setMessage}
            sendMessage={props.sendMessage}
            isRunning={props.isRunning}
            variant="hero"
          />
        </section>
      )}
      {hasMessages && (
        <>
          <div className="messages">
            {props.events.map((event, index) => (
              <div key={index} className={`message ${event.type} ${"status" in event ? event.status || "" : ""}`}>
                <span>{roleLabel(event.type)}</span>
                <MessageBody event={event} />
              </div>
            ))}
          </div>
          <PromptCard
            config={props.config}
            updateConfig={props.updateConfig}
            setNotice={props.setNotice}
            message={props.message}
            setMessage={props.setMessage}
            sendMessage={props.sendMessage}
            isRunning={props.isRunning}
            variant="composer"
          />
        </>
      )}
    </div>
  );
}

function MessageBody({ event }: { event: ChatEvent }) {
  if (event.type === "assistant") {
    return <AssistantMarkdown text={event.text || "思考中…"} />;
  }
  if (event.type === "tool") {
    return <ToolMessage event={event} />;
  }
  return <p className="message-body plain">{event.text}</p>;
}

function AssistantMarkdown({ text }: { text: string }) {
  return (
    <div className="message-body markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer" />
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}

function ToolMessage({ event }: { event: Extract<ChatEvent, { type: "tool" }> }) {
  const defaultOpen = event.status === "running" || event.status === "error" || event.status === "partial_success" || event.status === "rejected";
  return (
    <details className="tool-details" open={defaultOpen}>
      <summary className="tool-summary">
        <span>{toolSummary(event.text, event.status)}</span>
        {event.status && <span className={`tool-status ${event.status}`}>{event.status}</span>}
      </summary>
      <pre className="tool-output">{event.text}</pre>
    </details>
  );
}

function toolSummary(text: string, status?: string): string {
  if (status === "running") return text || "工具执行中";
  const firstLine = text.split("\n")[0]?.trim();
  return firstLine || "工具结果";
}

function PromptCard(props: {
  config: AppConfig | null;
  updateConfig: (patch: Partial<Pick<AppConfig, "cwd" | "mode">>) => Promise<void>;
  setNotice: (message: string) => void;
  message: string;
  setMessage: (value: string) => void;
  sendMessage: () => void;
  isRunning: boolean;
  variant: "hero" | "composer";
}) {
  const mode = props.config?.mode || "ReAct";
  const placeholder = mode === "plan" ? "描述你想规划的改动，mca 会先给出方案，不会改文件。" : "随心输入";

  async function changeMode(nextMode: AgentMode) {
    try {
      await props.updateConfig({ mode: nextMode });
    } catch (error) {
      props.setNotice((error as Error).message);
    }
  }

  return (
    <div className={`prompt-card ${props.variant}`}>
      <textarea
        className="prompt-textarea"
        value={props.message}
        onChange={(event) => props.setMessage(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            if (!props.isRunning && props.message.trim()) props.sendMessage();
          }
        }}
        placeholder={placeholder}
      />
      <div className="prompt-toolbar">
        <div className="prompt-toolbar-left">
          <ModeSelect mode={mode} onChange={changeMode} disabled={props.isRunning || !props.config} />
        </div>
        <div className="prompt-toolbar-right">
          <ModelChip config={props.config} />
          <button className="send-circle" onClick={props.sendMessage} disabled={props.isRunning || !props.message.trim()} title="发送">
            <Send size={20} />
          </button>
        </div>
      </div>
      <WorkspaceControl config={props.config} updateConfig={props.updateConfig} setNotice={props.setNotice} />
    </div>
  );
}

function ModeSelect(props: { mode: AgentMode; onChange: (mode: AgentMode) => Promise<void>; disabled?: boolean }) {
  return (
    <label className="mode-select">
      <Sparkles size={15} />
      <select value={props.mode} disabled={props.disabled} onChange={(event) => props.onChange(event.target.value as AgentMode)}>
        <option value="ReAct">Agent</option>
        <option value="plan">Plan</option>
      </select>
      <ChevronDown size={14} />
    </label>
  );
}

function ModelChip({ config }: { config: AppConfig | null }) {
  const label = config ? [config.model || config.provider, config.provider].filter(Boolean).join(" · ") : "加载中";
  return <span className="prompt-chip model-chip">{label}</span>;
}

function WorkspaceControl(props: {
  config: AppConfig | null;
  updateConfig: (patch: Partial<Pick<AppConfig, "cwd" | "mode">>) => Promise<void>;
  setNotice: (message: string) => void;
}) {
  const [isPicking, setIsPicking] = useState(false);
  const cwd = props.config?.cwd || "";

  async function pickWorkspace() {
    if (!props.config || isPicking) return;
    setIsPicking(true);
    try {
      const payload = await api<{ path: string }>("/api/dialog/directory", {
        method: "POST",
        body: JSON.stringify({ initial: cwd })
      });
      const selected = (payload.path || "").trim();
      if (!selected || selected === cwd) return;
      await props.updateConfig({ cwd: selected });
    } catch (error) {
      props.setNotice((error as Error).message);
    } finally {
      setIsPicking(false);
    }
  }

  if (!props.config) {
    return <div className="workspace-row"><span className="workspace-chip muted"><Folder size={15} /> 加载工作目录…</span></div>;
  }

  return (
    <div className="workspace-row">
      <button className="workspace-chip" onClick={pickWorkspace} disabled={isPicking} title={`选择工作目录：${cwd}`}>
        <Folder size={15} />
        <span>{isPicking ? "正在选择…" : basename(cwd)}</span>
        <ChevronDown size={14} />
      </button>
    </div>
  );
}

function CapabilitiesView({
  tools,
  skills,
  mcp,
  onToggle
}: {
  tools: ToolSpec[];
  skills: SkillSpec[];
  mcp: McpServer[];
  onToggle: (kind: "tools" | "skills" | "mcp", name: string, enabled: boolean) => Promise<void>;
}) {
  return (
    <div className="grid three">
      <section className="panel">
        <h2><Wrench size={18} /> 工具</h2>
        <div className="list">
          {tools.map((tool) => (
            <div className={`row ${tool.enabled ? "" : "disabled"}`} key={tool.name}>
              <div className="capability-title-row">
                <strong>{tool.name}</strong>
                <label className="capability-toggle">
                  <input type="checkbox" checked={tool.enabled} onChange={(event) => onToggle("tools", tool.name, event.target.checked)} />
                  <span>{tool.enabled ? "已启用" : "已禁用"}</span>
                </label>
              </div>
              <div className="capability-actions">
                <span className={tool.risky ? "badge risk" : "badge"}>{tool.risky ? "需审批" : "安全"}</span>
              </div>
              <p>{toolDescription(tool)}</p>
            </div>
          ))}
        </div>
      </section>
      <section className="panel">
        <h2><Sparkles size={18} /> Skill</h2>
        <div className="list">
          {skills.map((skill) => (
            <div className={`row ${skill.enabled ? "" : "disabled"}`} key={skill.name}>
              <div className="capability-title-row">
                <strong>{skill.name}</strong>
                <label className="capability-toggle">
                  <input type="checkbox" checked={skill.enabled} onChange={(event) => onToggle("skills", skill.name, event.target.checked)} />
                  <span>{skill.enabled ? "已启用" : "已禁用"}</span>
                </label>
              </div>
              <p>{skill.description}</p>
            </div>
          ))}
        </div>
      </section>
      <section className="panel">
        <h2><Plug size={18} /> MCP</h2>
        <div className="list">
          {mcp.map((server) => (
            <div className={`row ${server.enabled ? "" : "disabled"}`} key={server.name}>
              <div className="capability-title-row">
                <strong>{server.name}</strong>
                <label className="capability-toggle">
                  <input type="checkbox" checked={server.enabled} onChange={(event) => onToggle("mcp", server.name, event.target.checked)} />
                  <span>{server.enabled ? "已启用" : "已禁用"}</span>
                </label>
              </div>
              <p>{server.command} {server.args?.join(" ")}</p>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}

function MetricsView({ metrics }: { metrics: Metrics | null }) {
  if (!metrics) return <div className="empty">暂无指标数据。</div>;
  return (
    <div className="metrics-grid">
      <Metric label="总运行次数" value={fmtNumber(metrics.totalRuns)} />
      <Metric label="输入 token" value={fmtNumber(metrics.inputTokens)} />
      <Metric label="输出 token" value={fmtNumber(metrics.outputTokens)} />
      <Metric label="总 token" value={fmtNumber(metrics.totalTokens)} />
      <Metric label="缓存命中 token" value={fmtNumber(metrics.cachedTokens)} />
      <Metric label="缓存命中率" value={`${Math.round(metrics.cacheHitRate * 100)}%`} />
      <Metric label="平均工具步数" value={metrics.avgToolSteps.toFixed(1)} />
      <Metric label="平均尝试次数" value={metrics.avgAttempts.toFixed(1)} />
      <section className="panel status-panel">
        <h2><Database size={18} /> 状态分布</h2>
        {Object.entries(metrics.statusCounts).map(([key, value]) => (
          <div className="status-row" key={key}><span>{key}</span><strong>{value}</strong></div>
        ))}
      </section>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <section className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
    </section>
  );
}

function ImportsView({ onDone }: { onDone: () => Promise<unknown> }) {
  const [mcpJson, setMcpJson] = useState("{\n  \"servers\": []\n}");
  const [overwrite, setOverwrite] = useState(false);
  const [result, setResult] = useState("");

  async function uploadSkill(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    await api(`/api/imports/skills?confirmOverwrite=${overwrite}`, { method: "POST", body: form });
    setResult(`已导入 skill：${file.name}`);
    await onDone();
  }

  async function importMcp() {
    await api(`/api/imports/mcp?confirmOverwrite=${overwrite}`, { method: "POST", body: mcpJson });
    setResult("已导入 MCP 配置");
    await onDone();
  }

  return (
    <div className="imports-layout">
      <section className="panel">
        <h2><FileUp size={18} /> Skill 文件</h2>
        <label className="toggle"><input type="checkbox" checked={overwrite} onChange={(event) => setOverwrite(event.target.checked)} /> 同名覆盖</label>
        <input type="file" accept=".md,text/markdown" onChange={uploadSkill} />
      </section>
      <section className="panel">
        <h2><Plug size={18} /> MCP JSON</h2>
        <textarea className="json-input" value={mcpJson} onChange={(event) => setMcpJson(event.target.value)} />
        <button className="primary" onClick={importMcp}><Check size={16} /> 导入 MCP</button>
      </section>
      {result && <div className="notice success">{result}</div>}
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
