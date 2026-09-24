"use client";

import { useEffect, useRef, useState } from "react";

type PendingDraft = { id: string; direction: "income" | "expense"; amount_cents: number; category: string; channel: string; project?: string; project_name?: string };
type Msg = { role: "user" | "ai"; text: string; pending?: PendingDraft[] };
type StateT = {
  label: string;
  financial_health: number;
  cashflow_confidence: number;
  stability_days: number;
  events_count: number;
};
type Fc = {
  median_balance_cents: number;
  band90_low_cents: number;
  band90_high_cents: number;
  confidence: number;
  insufficient?: boolean;
  reason: string;
};
type Ev = { event_id: string; ts: string; type: string; amount_cents: number; category: string; channel: string; note: string };
/** 项目维度：账本只存稳定 id，显示名来自后端映射表，改名不会动账本。 */
type ProjectRow = {
  id: string;
  name: string;
  named: boolean;
  created: string;
  renamed_at: string;
  income_cents: number;
  expense_cents: number;
  count: number;
};
type ProjectsView = {
  ok: boolean;
  project_count: number;
  projects: ProjectRow[];
  unassigned: { project: string; name: string; income_cents: number; expense_cents: number; count: number };
  unnamed_project_ids: string[];
  naming_hint: string;
  disclaimer: string;
};

/** 拖进来的文件（rel_path 保留文件夹层级，第一层目录名就是项目名） */
type FileItem = { name: string; rel_path: string; file: File };
/** 浏览器拖放时拿到的文件系统条目（webkitGetAsEntry 未进标准类型，故自定义） */
type FsEntry = {
  isFile: boolean;
  isDirectory: boolean;
  name: string;
  file: (cb: (f: File) => void, err: (e: unknown) => void) => void;
  createReader: () => {
    readEntries: (cb: (entries: FsEntry[]) => void, err?: (e: unknown) => void) => void;
  };
};
type IntakeBatch = {
  source_folder: string;
  project_id: string;
  project_name: string;
  project_hint: string;
  draft_count: number;
  identified_total_cents: number;
  declared_total_cents: number;
  reconcile_diff_cents?: number;
  reconcile?: { checked: number; matched: number; mismatched: unknown[]; unreadable: unknown[] } | null;
  errors: { file?: string; error: string }[];
};
type IntakeResult = {
  ok: boolean;
  batches: IntakeBatch[];
  errors: { file?: string; error: string }[];
  draft_total_cents: number;
};
type ChatReply = { ok: boolean; text: string; session_id?: string; pending?: PendingDraft[] };
type ModelTier = {
  id: "cloud" | "local" | "none";
  label: string;
  desc: string;
  data_leaves_device: boolean;
  cost: string;
  active: boolean;
  vendor?: string | null;
  model?: string | null;
  preset_id?: string;
  preset_label?: string;
  base_url?: string;
  vision?: boolean;
  configured?: boolean;
  usable?: boolean;
};
type LocalPreset = {
  id: string;
  label: string;
  vendor: string;
  base_url: string;
  model: string;
  vision: boolean | null;
  note?: string;
};
type ModelsView = {
  tier: "cloud" | "local" | "none";
  active: { tier: string; model: string; vision: boolean; usable: boolean };
  tiers: ModelTier[];
  local_presets: LocalPreset[];
  warnings: string[];
  api_key_configured: boolean;
  privacy_note: string;
};

const yuan = (c: number) =>
  `¥${(c / 100).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const CHANNEL_CN: Record<string, string> = { wechat: "微信", alipay: "支付宝", cash: "现金", bank: "银行卡", manual: "手动", voice: "语音", receipt: "票据" };
const LABEL_CN: Record<string, string> = {
  unknown: "现金流不明",
  learning: "学习中",
  fragile: "现金流脆弱",
  review_due: "需复查",
  stable: "稳健",
  misconception: "财务误区",
};

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json() as Promise<T>;
}

/** 会话 id：localStorage 持久化，多轮追问共享同一上下文。
 *  key 带 v2：数据清零时一并作废旧会话（旧 key 的历史上下文不再被读到）。 */
function sessionId(): string {
  if (typeof window === "undefined") return "";
  let s = window.localStorage.getItem("cl_session_v2");
  if (!s) {
    s = window.crypto?.randomUUID?.() ?? `s-${Date.now()}`;
    window.localStorage.setItem("cl_session_v2", s);
  }
  return s;
}

export default function Workbench() {
  // 空会话占位 · 开场白 B+ 版（2026-09-06 定稿：B 极简上手型为基底，加长+增人情味 · 与 backend _PERSONA_PROMPT 同源，规格见 02-脑暴/对话人格层规格-20260906.md 第四节）
  // 首屏固定默认开场白（与服务端渲染一致，避免 hydration mismatch）；历史消息在挂载后从 localStorage 加载
  const [msgs, setMsgs] = useState<Msg[]>(() => [
    {
      role: "ai",
      text: "你好，我是 CashLens 财务管家。钱的事你不用懂格式，也不用自己记流水账——跟我说大白话就行，剩下的归拢、分类、盯缺口，都交给我。\n\n比如一句「昨天微信收了 3000 尾款」或「打车花了 28」，我马上帮你记好；想知道下个月会不会缺钱，就问我「现金流怎么样」，我会把依据一起讲给你听。\n\n先来一句试试？\n\n（本地开发提示：需先启动后端 cd backend && python3 -m uvicorn app.main:app --port 8001）",
    },
  ]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState<StateT | null>(null);
  const [fc, setFc] = useState<Fc | null>(null);
  const [events, setEvents] = useState<Ev[]>([]);
  const [apiOk, setApiOk] = useState(true);
  const [llmOn, setLlmOn] = useState(false);
  const [models, setModels] = useState<ModelsView | null>(null);
  const [pickOpen, setPickOpen] = useState(false);
  const [modelBusy, setModelBusy] = useState(false);
  const [modelMsg, setModelMsg] = useState("");
  // 自定义本地端点的内联表单（不填就没法用，所以必须给入口）
  const [customEditing, setCustomEditing] = useState(false);
  const [customUrl, setCustomUrl] = useState("");
  const [customModel, setCustomModel] = useState("");
  const [customVision, setCustomVision] = useState(true);
  // 项目维度：列表 + 内联改名（改名只改显示名映射，账本不动）
  const [projView, setProjView] = useState<ProjectsView | null>(null);
  const [renamingId, setRenamingId] = useState("");
  const [renameText, setRenameText] = useState("");
  const [projMsg, setProjMsg] = useState("");
  // 新建项目：内联表单，成功后刷新左侧项目面板
  const [creatingProj, setCreatingProj] = useState(false);
  const [newProjName, setNewProjName] = useState("");
  // 登录：原型阶段只在本地记一个用户名，不接账号体系；不登录也能用全部功能
  const [user, setUser] = useState("");
  const [loginOpen, setLoginOpen] = useState(false);
  const [nameText, setNameText] = useState("");
  // 导入：拖文件 / 拖整个文件夹（按文件夹名建项目）
  const [dragActive, setDragActive] = useState(false);
  const [intakeBusy, setIntakeBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);
  const logRef = useRef<HTMLDivElement>(null);

  async function refresh() {
    try {
      const [s, f, e, c, m] = await Promise.all([
        j<{ state: StateT }>("/api/state"),
        j<Fc>("/api/forecast?horizon_days=30"),
        j<{ events: Ev[] }>("/api/events?limit=20"),
        j<{ llm: boolean }>("/api/capabilities"),
        j<ModelsView>("/api/models"),
      ]);
      setState(s.state);
      setFc(f);
      setEvents(e.events);
      setLlmOn(!!c.llm);
      setModels(m);
      setApiOk(true);
    } catch {
      setApiOk(false);
    }
    // 项目面板单独取：它失败不该把整个工作台一起拖黑（例如后端进程还是旧版本，没有 /api/projects）
    try {
      setProjView(await j<ProjectsView>("/api/projects"));
    } catch {
      setProjView(null);
    }
  }

  /** 切换模型档位：运行中立即生效，不用重启后端。 */
  async function pickModel(tier: ModelTier["id"]) {
    if (modelBusy || !models || models.tier === tier) {
      setPickOpen(false);
      return;
    }
    setModelBusy(true);
    setModelMsg("");
    try {
      const next = await j<ModelsView>("/api/models/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tier }),
      });
      setModels(next);
      setLlmOn(!!next.active.usable);
      const label = next.tiers.find((t) => t.id === tier)?.label ?? tier;
      setModelMsg(`已切到 ${label}`);
      setPickOpen(false);
    } catch {
      setModelMsg("切换失败，请确认后端在运行");
    } finally {
      setModelBusy(false);
    }
  }

  /** 选具体本地预设（Ollama 各家模型或自定义端点）。 */
  async function pickLocalPreset(presetId: string) {
    if (modelBusy) return;
    // 选「自定义」不立即提交：先把当前值带进表单，让用户填完再存
    if (presetId === "custom") {
      const loc = models?.tiers.find((t) => t.id === "local");
      setCustomUrl(loc?.base_url || "");
      setCustomModel(loc?.model || "");
      setCustomVision(loc?.vision !== false);
      setCustomEditing(true);
      return;
    }
    setModelBusy(true);
    setModelMsg("");
    try {
      const next = await j<ModelsView>("/api/models/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tier: "local", preset_id: presetId }),
      });
      setModels(next);
      setLlmOn(!!next.active.usable);
      const preset = next.local_presets.find((p) => p.id === presetId);
      setModelMsg(
        next.active.usable
          ? `已切到本地模型：${preset?.label ?? presetId}`
          : "已选预设，但端点信息不完整，请先配置"
      );
      setCustomEditing(false);
      setPickOpen(false);
    } catch {
      setModelMsg("切换失败，请确认后端在运行");
    } finally {
      setModelBusy(false);
    }
  }

  /** 保存自定义本地端点并启用。 */
  async function saveCustomEndpoint() {
    if (modelBusy) return;
    if (!customUrl.trim() || !customModel.trim()) {
      setModelMsg("端点地址与模型名都要填");
      return;
    }
    setModelBusy(true);
    setModelMsg("");
    try {
      const next = await j<ModelsView>("/api/models/select", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          tier: "local",
          preset_id: "custom",
          base_url: customUrl.trim(),
          model: customModel.trim(),
          declared_vision: customVision,
        }),
      });
      setModels(next);
      setLlmOn(!!next.active.usable);
      setModelMsg("已启用自定义本地端点");
      setCustomEditing(false);
      setPickOpen(false);
    } catch {
      setModelMsg("保存失败，请确认后端在运行");
    } finally {
      setModelBusy(false);
    }
  }

  /** 当前档位的短标签：界面只露一行，完整型号在菜单与 title 里。 */
  function currentModelLabel(): string {
    if (!models) return "模型加载中…";
    const cur = models.tiers.find((t) => t.id === models.tier);
    if (!cur) return models.tier;
    if (cur.id === "none") return "不用模型";
    if (cur.id === "local" && cur.preset_id === "custom") return cur.model || "自定义端点";
    return cur.preset_label || cur.model || cur.label;
  }

  useEffect(() => {
    refresh();
  }, []);

  useEffect(() => {
    // 用户名只存在本地（原型阶段无账号体系，不登录也能用全部功能）
    if (typeof window === "undefined") return;
    try {
      const u = window.localStorage.getItem("cl_user");
      if (u) setUser(u);
    } catch {
      /* 忽略存储失败 */
    }
  }, []);

  useEffect(() => {
    // 挂载后再从 localStorage 恢复历史（与服务端渲染解耦，修复 hydration mismatch）
    if (typeof window === "undefined") return;
    try {
      const raw = window.localStorage.getItem("cl_msgs_v2");
      if (raw) {
        const arr: unknown = JSON.parse(raw);
        if (Array.isArray(arr)) {
          const ok = arr
            .filter((x) => !!x && typeof x === "object" && (x as Msg).role !== undefined && typeof (x as Msg).text === "string")
            .map((x) => ({ role: (x as Msg).role, text: (x as Msg).text }));
          if (ok.length > 0) setMsgs(ok);
        }
      }
    } catch {
      /* 忽略损坏缓存 */
    }
  }, []);

  useEffect(() => {
    // 对话历史持久化（刷新不丢；只存文本，最近 40 条）
    try {
      if (typeof window !== "undefined") {
        window.localStorage.setItem(
          "cl_msgs_v2",
          JSON.stringify(msgs.slice(-40).map((m) => ({ role: m.role, text: m.text })))
        );
      }
    } catch {
      /* 忽略存储失败 */
    }
  }, [msgs]);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs]);

  const [actingIds, setActingIds] = useState<Set<string>>(new Set());

  async function act(p: PendingDraft, acceptIt: boolean) {
    if (actingIds.has(p.id)) return; // 防连点：处理中不可重复提交
    setActingIds((prev) => new Set(prev).add(p.id));
    try {
      await j<{ ok: boolean }>(`/api/pending/${p.id}/${acceptIt ? "accept" : "decline"}`, { method: "POST" });
      // 成功即从气泡移除该草稿按钮（已入账/已忽略的不允许再点）
      setMsgs((m) =>
        m.map((msg) =>
          msg.pending && msg.pending.some((x) => x.id === p.id)
            ? { ...msg, pending: msg.pending.filter((x) => x.id !== p.id) }
            : msg
        )
      );
      setMsgs((m) => [
        ...m,
        { role: "ai", text: acceptIt
            ? `已确认入账 ${yuan(p.amount_cents)}（${p.category}）${p.project_name ? `，归入「${p.project_name}」` : ""}。`
            : "已忽略，这笔不入账。" },
      ]);
      refresh();
    } catch {
      setMsgs((m) => [...m, { role: "ai", text: "操作失败：请确认后端在运行（cd backend && python3 -m uvicorn app.main:app --port 8001）。" }]);
    } finally {
      setActingIds((prev) => {
        const next = new Set(prev);
        next.delete(p.id);
        return next;
      });
    }
  }

  /** 项目改名：只改后端映射表里的显示名，账本里的事件一字不动。 */
  async function saveRename() {
    const name = renameText.trim();
    if (!renamingId || !name) return;
    try {
      await j<{ ok: boolean }>("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: renamingId, name }),
      });
      setProjMsg(`已改名为「${name}」，账本记录不受影响。`);
      setRenamingId("");
      setRenameText("");
      refresh();
    } catch {
      setProjMsg("改名失败：请确认后端在运行。");
    }
  }

  /** 保存用户名：只写本地存储；清空后保存即视为退出登录。 */
  function saveUser() {
    const name = nameText.trim();
    try {
      if (name) {
        window.localStorage.setItem("cl_user", name);
      } else {
        window.localStorage.removeItem("cl_user");
      }
    } catch {
      /* 忽略存储失败 */
    }
    setUser(name);
    setLoginOpen(false);
    setNameText("");
  }

  /** 新建项目：只写后端映射表（账本只存稳定 id），建完刷新左侧面板。 */
  async function createProject() {
    const name = newProjName.trim();
    if (!name) {
      setProjMsg("给项目起个名字，例如「919 昆明项目」");
      return;
    }
    try {
      await j<{ ok: boolean }>("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      setProjMsg(`已新建「${name}」。记账时说一句就能归到它下面。`);
      setCreatingProj(false);
      setNewProjName("");
      refresh();
    } catch {
      setProjMsg("新建失败：请确认后端在运行。");
    }
  }

  /** 递归展开拖进来的目录（拖整个文件夹时，第一层目录名会成为项目名）。 */
  async function readEntry(entry: FsEntry, prefix: string): Promise<FileItem[]> {
    if (entry.isFile) {
      const file = await new Promise<File>((res, rej) => entry.file(res, rej));
      return [{ name: file.name, rel_path: `${prefix}${file.name}`, file }];
    }
    if (!entry.isDirectory) return [];
    const reader = entry.createReader();
    const all: FsEntry[] = [];
    for (;;) {
      const batch = await new Promise<FsEntry[]>((res, rej) => reader.readEntries(res, rej));
      if (!batch.length) break;
      all.push(...batch);
    }
    const out: FileItem[] = [];
    for (const e of all) out.push(...(await readEntry(e, `${prefix}${entry.name}/`)));
    return out;
  }

  /** 从拖放事件取文件；是文件夹就递归展开。 */
  async function filesFromDrop(dt: DataTransfer): Promise<FileItem[]> {
    const entries = Array.from(dt.items || [])
      .map((it) => (it as unknown as { webkitGetAsEntry?: () => FsEntry | null }).webkitGetAsEntry?.() ?? null)
      .filter((e): e is FsEntry => !!e);
    if (entries.some((e) => e.isDirectory)) {
      const out: FileItem[] = [];
      for (const e of entries) out.push(...(await readEntry(e, "")));
      return out;
    }
    return Array.from(dt.files).map((f) => ({ name: f.name, rel_path: f.name, file: f }));
  }

  /** File → base64（分块拼接，避免大文件一次性展开爆栈）。 */
  async function fileToB64(file: File): Promise<string> {
    const buf = new Uint8Array(await file.arrayBuffer());
    let bin = "";
    const CHUNK = 0x8000;
    for (let i = 0; i < buf.length; i += CHUNK) {
      bin += String.fromCharCode(...Array.from(buf.subarray(i, i + CHUNK)));
    }
    return window.btoa(bin);
  }

  /** 导入：上传 → 把摘要写进对话流 → 刷新面板。 */
  async function uploadFiles(items: FileItem[]) {
    if (!items.length || intakeBusy) return;
    setIntakeBusy(true);
    setDragActive(false);
    setMsgs((m) => [...m, { role: "user", text: `（导入 ${items.length} 个文件）` }]);
    try {
      const files = await Promise.all(items.map(async (it) => ({
        name: it.name,
        rel_path: it.rel_path,
        content_b64: await fileToB64(it.file),
      })));
      const r = await j<IntakeResult>("/api/intake", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ files }),
      });
      const lines: string[] = [];
      for (const b of r.batches) {
        lines.push(`【${b.project_name}】生成 ${b.draft_count} 条待确认草稿，合计 ${yuan(b.identified_total_cents)}`);
        if (b.declared_total_cents) {
          const diff = b.reconcile_diff_cents ?? 0;
          lines.push(`  表内总计 ${yuan(b.declared_total_cents)}，差额 ${yuan(diff)}${diff === 0 ? "（对得上）" : "（有没认出来的，请核对）"}`);
        }
        if (b.reconcile) {
          lines.push(`  双源核对：${b.reconcile.matched}/${b.reconcile.checked} 张与表内金额一致`);
        }
        if (b.project_hint) lines.push(`  ${b.project_hint}`);
        for (const e of b.errors) lines.push(`  ⚠️ ${e.file ?? ""}：${e.error}`);
      }
      setMsgs((m) => [...m, { role: "ai", text: lines.join("\n") || "没有可导入的内容。" }]);
      refresh();
    } catch (err) {
      setMsgs((m) => [...m, { role: "ai", text: `导入失败：${err instanceof Error ? err.message : String(err)}` }]);
    } finally {
      setIntakeBusy(false);
    }
  }

  async function send() {
    const t = text.trim();
    if (!t || busy) return;
    setMsgs((m) => [...m, { role: "user", text: t }]);
    setText("");
    setBusy(true);
    try {
      const r = await j<ChatReply>("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: t, session_id: sessionId() }),
      });
      if (r.session_id && typeof window !== "undefined") {
        window.localStorage.setItem("cl_session_v2", r.session_id);
      }
      setMsgs((m) => [...m, { role: "ai", text: r.text, pending: r.pending }]);
    } catch {
      setMsgs((m) => [...m, { role: "ai", text: "连不上本地后端：请先运行 cd backend && python3 -m uvicorn app.main:app --port 8001。" }]);
      setApiOk(false);
    } finally {
      setBusy(false);
      refresh();
    }
  }

  const healthPct = state ? Math.round(state.financial_health * 100) : 0;
  const confPct = state ? Math.round(state.cashflow_confidence * 100) : 0;

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <div className="brand">
            <span className="brand-mark" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.4" strokeLinecap="round">
                <circle cx="11" cy="11" r="7" />
                <line x1="16.5" y1="16.5" x2="21" y2="21" />
              </svg>
            </span>
            CashLens <span className="brand-sub">本地工作台 · 真数据</span>
          </div>
          <div className="topbar-right">
            <span className={`tag ${state ? `t-${state.label}` : "t-unknown"}`}>
              <span className="dot" />
              {state ? LABEL_CN[state.label] ?? state.label : apiOk ? "载入中…" : "后端未连接"}
            </span>
            {loginOpen ? (
              <span className="login-edit">
                <input
                  value={nameText}
                  onChange={(e) => setNameText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") saveUser();
                    if (e.key === "Escape") setLoginOpen(false);
                  }}
                  placeholder="你的名字，如「小李」"
                  aria-label="用户名"
                  autoFocus
                />
                <button className="btn-mini ok" onClick={saveUser}>保存</button>
                <button className="btn-mini" onClick={() => setLoginOpen(false)}>取消</button>
              </span>
            ) : (
              <button
                className="btn-mini"
                onClick={() => { setNameText(user); setLoginOpen(true); }}
                title={user ? "点击修改用户名；清空后保存即退出" : "输入一个用户名，只存在这台电脑上"}
              >
                {user || "登录"}
              </button>
            )}
          </div>
        </div>
      </header>

      <div className="main">
        <div className="grid">
          {/* 对话区：支持拖入文件 / 整个文件夹 */}
          <section
            className="card convo"
            aria-label="对话记账与票据导入"
            onDragOver={(e) => { e.preventDefault(); if (!intakeBusy) setDragActive(true); }}
            onDragLeave={(e) => { if (e.currentTarget === e.target) setDragActive(false); }}
            onDrop={async (e) => {
              e.preventDefault();
              if (intakeBusy) return;
              const items = await filesFromDrop(e.dataTransfer);
              await uploadFiles(items);
            }}
          >
            {dragActive && (
              <div className="drop-overlay" aria-hidden="true">
                <b>松手即导入</b>
                <span>图片 / PDF / Excel / CSV 都行；拖整个文件夹就按文件夹名建项目</span>
              </div>
            )}
            <div className="convo-head">
              <span className="t">说一句，钱就记下了</span>
              <div className="head-right">
                <span className="muted">
                  {state ? `${state.events_count} 笔 · 本地账本 · ` : ""}
                  {llmOn ? "LLM 对话" : "规则层"}
                </span>
                <button className="btn-mini" onClick={() => fileRef.current?.click()} disabled={!apiOk || intakeBusy}>
                  {intakeBusy ? "导入中…" : "导入票据"}
                </button>
                <input
                  ref={fileRef}
                  type="file"
                  multiple
                  hidden
                  accept=".jpg,.jpeg,.png,.webp,.gif,.pdf,.xlsx,.xlsm,.csv"
                  onChange={async (e) => {
                    const fs = Array.from(e.target.files ?? []);
                    e.target.value = "";
                    if (fs.length) await uploadFiles(fs.map((f) => ({ name: f.name, rel_path: f.name, file: f })));
                  }}
                />
              </div>
            </div>
            <div className="log" ref={logRef}>
              {msgs.map((m, i) => (
                <div key={i} className={`b ${m.role}`}>
                  {m.text}
                  {m.pending && m.pending.length > 0 && (
                    <div className="acts">
                      {m.pending.map((p) => (
                        <div className="act-row" key={p.id}>
                          <span className="num">
                            {p.direction === "income" ? "收入" : "支出"} {yuan(p.amount_cents)}（{p.category}）
                            {p.project_name ? ` → ${p.project_name}` : ""}
                          </span>
                          <button className="btn-mini ok" disabled={actingIds.has(p.id)} onClick={() => act(p, true)}>
                            {actingIds.has(p.id) ? "处理中…" : "确认入账"}
                          </button>
                          <button className="btn-mini" disabled={actingIds.has(p.id)} onClick={() => act(p, false)}>
                            {actingIds.has(p.id) ? "处理中…" : "不要这笔"}
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
            <div className="inputrow">
              <input
                value={text}
                onChange={(e) => setText(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && send()}
                placeholder='记账或问现金流，如「打车花了 28」「我下个月现金流怎么样」'
                aria-label="输入一句话"
                disabled={!apiOk}
              />
              <button className="btn" onClick={send} disabled={!apiOk || busy}>
                {busy ? "处理中…" : "发送"}
              </button>
            </div>
            <div className="inputfoot">
              <div className="hint">
                LLM 语义理解 + 规则兜底 → 事件账本 → 状态引擎真计算；金额与状态均为本地真实数据。
                {models?.tier === "cloud" && "当前档位为云端，自由对话文本与票据图像会上云。"}
                {models?.tier === "local" && "当前用本地模型，数据不出本机。"}
                {models?.tier === "none" && "当前不调用模型，全部本机处理。"}
              </div>
              <div className="modelrow">
                <button
                  className="model-pick"
                  onClick={() => setPickOpen((v) => !v)}
                  disabled={!apiOk || modelBusy}
                  aria-haspopup="listbox"
                  aria-expanded={pickOpen}
                  title={models?.active.model ? `当前模型：${models.active.model}` : undefined}
                >
                  <span
                    className={`dot ${
                      models?.tier === "cloud" ? "cloud" : models?.tier === "none" ? "off" : ""
                    }`}
                  />
                  {modelBusy ? "切换中…" : currentModelLabel()}
                  <span className="caret" aria-hidden="true">▾</span>
                </button>
                {pickOpen && models && (
                  <div className="model-menu" role="group" aria-label="选择模型档位">
                    {models.tiers.map((t) => (
                      <div key={t.id}>
                        <button
                          className={`model-item${t.active ? " on" : ""}`}
                          aria-pressed={t.active}
                          onClick={() => pickModel(t.id)}
                        >
                          <span className="mi-head">
                            {t.label}
                            {t.vendor && <span className="mi-vendor">{t.vendor}</span>}
                          </span>
                          <span className="mi-meta">
                            <span>{t.data_leaves_device ? "数据出本机" : "数据不出本机"}</span>
                            <span>{t.cost}</span>
                            {t.id !== "none" && t.vision === false && (
                              <span className="mi-warn">不支持图像</span>
                            )}
                          </span>
                        </button>
                        {t.id === "local" && (
                          <>
                            <div className="model-sub">
                              {models.local_presets.map((p) => (
                                <button
                                  key={p.id}
                                  className={`model-subitem${
                                    t.active && t.preset_id === p.id && !customEditing ? " on" : ""
                                  }`}
                                  onClick={() => pickLocalPreset(p.id)}
                                >
                                  <span>{p.label}</span>
                                  {p.vision === null && <span className="mi-warn">需确认</span>}
                                  {p.vision === false && <span className="mi-warn">无图像</span>}
                                </button>
                              ))}
                            </div>
                            {customEditing && (
                              <div className="model-form">
                                <label>
                                  <span>端点地址</span>
                                  <input
                                    value={customUrl}
                                    onChange={(e) => setCustomUrl(e.target.value)}
                                    placeholder="http://127.0.0.1:1234/v1"
                                    aria-label="本地推理服务端点地址"
                                  />
                                </label>
                                <label>
                                  <span>模型名</span>
                                  <input
                                    value={customModel}
                                    onChange={(e) => setCustomModel(e.target.value)}
                                    placeholder="qwen2.5-vl:7b"
                                    aria-label="模型名"
                                  />
                                </label>
                                <label className="model-check">
                                  <input
                                    type="checkbox"
                                    checked={customVision}
                                    onChange={(e) => setCustomVision(e.target.checked)}
                                  />
                                  该模型支持图像输入（不支持则票据识别不可用）
                                </label>
                                <button
                                  className="btn-mini ok"
                                  onClick={saveCustomEndpoint}
                                  disabled={modelBusy}
                                >
                                  保存并使用
                                </button>
                              </div>
                            )}
                          </>
                        )}
                      </div>
                    ))}
                    {models.warnings.length > 0 && (
                      <div className="model-note">{models.warnings[0]}</div>
                    )}
                    <div className="model-msg">{models.privacy_note}</div>
                  </div>
                )}
              </div>
            </div>
            {modelMsg && <div className="hint">{modelMsg}</div>}
          </section>

          {/* 右侧面板 */}
          <aside className="side" aria-label="现金流面板">
            {/* 项目维度：账本按项目归集，这里是项目的唯一入口 */}
            <section className="card sect">
              <div className="sect-head">
                <h3>项目（按项目归集）</h3>
                <button
                  className="btn-mini"
                  onClick={() => { setCreatingProj(true); setNewProjName(""); setProjMsg(""); }}
                  disabled={!apiOk}
                >
                  新建项目
                </button>
              </div>
              {creatingProj && (
                <div className="proj-edit">
                  <input
                    value={newProjName}
                    onChange={(e) => setNewProjName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") createProject();
                      if (e.key === "Escape") setCreatingProj(false);
                    }}
                    placeholder="项目全名，如「919 昆明项目」"
                    aria-label="新项目名称"
                    autoFocus
                  />
                  <button className="btn-mini ok" onClick={createProject}>创建</button>
                  <button className="btn-mini" onClick={() => setCreatingProj(false)}>取消</button>
                </div>
              )}
              {!projView || (projView.project_count === 0 && projView.unassigned.count === 0) ? (
                <div className="empty">还没有项目。点上面的「新建项目」，或记账时说一句「这笔记到 919 昆明项目」，就建好了</div>
              ) : (
                <>
                  {projView.projects.map((p) => (
                    <div key={p.id}>
                      <div className="ev">
                        <div>
                          <b>{p.name}</b>
                          <span className="m">
                            支出 {yuan(p.expense_cents)} · 收入 {yuan(p.income_cents)} · {p.count} 笔
                          </span>
                          {!p.named && <span className="m">默认名，建议改成这个项目的完整名称</span>}
                        </div>
                        <button
                          className="btn-mini"
                          onClick={() => {
                            setRenamingId(p.id);
                            setRenameText(p.name);
                            setProjMsg("");
                          }}
                        >
                          改名
                        </button>
                      </div>
                      {renamingId === p.id && (
                        <div className="proj-edit">
                          <input
                            value={renameText}
                            onChange={(e) => setRenameText(e.target.value)}
                            onKeyDown={(e) => e.key === "Enter" && saveRename()}
                            placeholder="这个项目的完整名称，如「919 昆明项目」"
                            aria-label="项目完整名称"
                          />
                          <button className="btn-mini ok" onClick={saveRename}>保存</button>
                          <button className="btn-mini" onClick={() => setRenamingId("")}>取消</button>
                        </div>
                      )}
                    </div>
                  ))}
                  {projView.unassigned.count > 0 && (
                    <div className="ev">
                      <div>
                        <b>{projView.unassigned.name}</b>
                        <span className="m">
                          支出 {yuan(projView.unassigned.expense_cents)} · {projView.unassigned.count} 笔，还没归项目
                        </span>
                      </div>
                    </div>
                  )}
                  {projView.naming_hint && <div className="fc-note">{projView.naming_hint}</div>}
                </>
              )}
              {projMsg && <div className="fc-note">{projMsg}</div>}
            </section>

            <section className="card sect">
              <div className="sect-head">
                <h3>财务状态（状态引擎 · 真）</h3>
              </div>
              <div className="kpi">
                <div className="cell">
                  <div className="v num">{healthPct}%</div>
                  <div className="k">健康度</div>
                </div>
                <div className="cell">
                  <div className="v num">{confPct}%</div>
                  <div className="k">现金流可信度</div>
                </div>
              </div>
              <div className="bar good">
                <i style={{ width: `${healthPct}%` }} />
              </div>
              <div className="bar">
                <i style={{ width: `${confPct}%` }} />
              </div>
            </section>

            <section className="card sect">
              <div className="sect-head">
                <h3>未来 30 天现金流（90% 区间）</h3>
              </div>
              {fc ? (
                <>
                  <div className="fc-row">
                    <span className="k">期末预计（中位）</span>
                    <b className="num">{yuan(fc.median_balance_cents)}</b>
                  </div>
                  <div className="fc-row">
                    <span className="k">区间下沿</span>
                    <b className={`num ${fc.band90_low_cents < 0 ? "amt-out" : "amt-in"}`}>{yuan(fc.band90_low_cents)}</b>
                  </div>
                  <div className="fc-row">
                    <span className="k">区间上沿</span>
                    <b className="num">{yuan(fc.band90_high_cents)}</b>
                  </div>
                  <div className="fc-note">
                    {fc.insufficient
                      ? "数据不足：区间暂不可信 —— 多记或导入几笔后自动变宽（现金流不明口径）"
                      : `${fc.reason} · 可信度低时如实标注「现金流不明」`}
                  </div>
                </>
              ) : (
                <div className="empty">{apiOk ? "等待数据…" : "后端未连接"}</div>
              )}
            </section>

            <section className="card sect">
              <div className="sect-head">
                <h3>最近事件</h3>
              </div>
              {events.length === 0 ? (
                <div className="empty">账本为空——说一句记账试试</div>
              ) : (
                events.slice(0, 12).map((ev) => (
                  <div className="ev" key={ev.event_id}>
                    <div>
                      <b className={ev.type === "expense" ? "amt-out" : "amt-in"}>{yuan(ev.amount_cents)}</b>
                      <span className="m">
                        {ev.category || "其他"} · {CHANNEL_CN[ev.channel] ?? ev.channel} · {ev.ts.slice(0, 10)}
                      </span>
                    </div>
                  </div>
                ))
              )}
            </section>
          </aside>
        </div>
        <p className="footnote">CashLens · 本地优先 · 数据留在磁盘 · 证据驱动（R38 纪律：此处每个数字都来自真实计算）</p>
      </div>
    </>
  );
}
