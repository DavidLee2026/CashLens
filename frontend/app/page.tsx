"use client";

import { useEffect, useRef, useState, type ChangeEvent } from "react";

type PendingDraft = { id: string; direction: "income" | "expense"; amount_cents: number; category: string; channel: string; project?: string; project_name?: string };
/** 总账户合计（全部项目 ＋ 未归项目）：左栏第一行用，也是"查询口径"的那本账。 */
function accountTotals(v: ProjectsView | null) {
  const rows = v?.projects ?? [];
  return {
    income: rows.reduce((s, x) => s + x.income_cents, 0) + (v?.unassigned.income_cents ?? 0),
    expense: rows.reduce((s, x) => s + x.expense_cents, 0) + (v?.unassigned.expense_cents ?? 0),
    count: rows.reduce((s, x) => s + x.count, 0) + (v?.unassigned.count ?? 0),
  };
}

/** 一个文件处理过程的四个阶段（顺序固定，见 STAGE_LABELS）。 */
type IntakeStages = { read: string; recognized: string; processed: string; how: string };
/** 对话里的一块进度：一个文件 + 它的四个阶段。 */
type ProgressBlock = { file: string; done: boolean; stages: IntakeStages };
type Msg = {
  role: "user" | "ai"; text: string; pending?: PendingDraft[];
  progress?: boolean; blocks?: ProgressBlock[];
};
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
  /** false 表示数据不足、后端没有做外推（此时三档数值是「当前已知净额」，不是 30 天预测）。 */
  extrapolated?: boolean;
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

/** 处理过程的四个阶段：顺序与标签集中在这里，前端不再各处硬写。 */
const STAGE_LABELS: [keyof IntakeStages, string][] = [
  ["read", "读取"], ["recognized", "识别"], ["processed", "处理"], ["how", "方式"],
];

/** 流式导入的进度事件（NDJSON 一行一个，对应后端 POST /api/intake/stream）。 */
type IntakeEvent =
  | { stage: "start"; index: number; total: number; file: string; rel_path: string }
  | { stage: "tick"; index: number; total: number; file: string; elapsed: number; note: string }
  | { stage: "file_done"; index: number; total: number; file: string; project_name: string;
      draft_count: number; identified_total_cents: number; stages?: IntakeStages; errors?: string[] }
  | { stage: "file_failed"; index: number; total: number; file: string; error: string }
  | { stage: "all_done"; batches: IntakeBatch[] };

/** 导入结果汇总：一个项目一段（沿用原有文案与口径，流式和非流式共用）。 */
function summarizeIntake(batches: IntakeBatch[], includeErrors = true): string[] {
  const lines: string[] = [];
  for (const b of batches) {
    if (b.draft_count > 0) {
      lines.push(`【${b.project_name}】生成 ${b.draft_count} 条待确认草稿，合计 ${yuan(b.identified_total_cents)}`);
    }
    if (b.declared_total_cents) {
      const diff = b.reconcile_diff_cents ?? 0;
      lines.push(`  表内总计 ${yuan(b.declared_total_cents)}，差额 ${yuan(diff)}${diff === 0 ? "（对得上）" : "（有没认出来的，请核对）"}`);
    }
    if (b.reconcile) {
      lines.push(`  双源核对：${b.reconcile.matched}/${b.reconcile.checked} 张与表内金额一致`);
    }
    if (b.project_hint) lines.push(`  ${b.project_hint}`);
    // 流式导入时错误已经逐文件内联报过（见 uploadFiles），这里别再重复一遍
    if (includeErrors) for (const e of b.errors) lines.push(`  ⚠️ ${e.file ?? ""}：${e.error}`);
  }
  return lines;
}

const CHANNEL_CN: Record<string, string> = { wechat: "微信", alipay: "支付宝", cash: "现金", bank: "银行卡", manual: "手动", voice: "语音", receipt: "票据" };
const LABEL_CN: Record<string, string> = {
  unknown: "现金流不明",
  learning: "数据积累中", // 原为「学习中」：放顶栏时看不明白，语义是引擎还在积累你的经营节奏
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

/** 指标旁的「?」：悬停或点击都显示一句人话解释。
 *  起因：连开发者自己都要回头查的指标（健康度、区间下沿），不该让客户去猜。 */
function Hint({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="hint-q">
      <button
        type="button"
        className="q"
        aria-label="这是什么意思"
        aria-expanded={open}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onClick={() => setOpen((v) => !v)}
        onBlur={() => setOpen(false)}
      >
        ?
      </button>
      {open && (
        <span className="q-tip" role="tooltip">
          {text}
        </span>
      )}
    </span>
  );
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
      text: "你好，我是 CashLens 财务管家。不用懂会计，也不用记格式，把花钱和收钱的事用大白话讲给我就行。\n\n我能帮你做三件事：\n1. 记账：说一句「昨天微信收了 3000 尾款」或「打车花了 28」，我记下来，还能记到对应项目上；\n2. 收票据：点左下角的「＋」，发票照片、发票 PDF、项目工时表、微信支付宝账单都能传，我先认出来给你看，你确认了才入账；\n3. 看钱够不够：问我「这个月花了多少」「下个月会不会缺钱」，我用你自己的账本算，并把依据一起说清楚。\n\n先来一句试试？",
    },
  ]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState<StateT | null>(null);
  const [fc, setFc] = useState<Fc | null>(null);
  const [events, setEvents] = useState<Ev[]>([]);
  const [apiOk, setApiOk] = useState(true);
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
  // 删除项目：点 × 先出选择（只删项目 / 连同账单一起删），不直接删
  const [deletingId, setDeletingId] = useState("");
  // 关于弹窗：版本 / 产品 / 开发者 / 联系方式（底部灰色小按钮打开）
  const [aboutOpen, setAboutOpen] = useState(false);
  // 新建项目：内联表单，成功后刷新左侧项目面板
  const [creatingProj, setCreatingProj] = useState(false);
  const [newProjName, setNewProjName] = useState("");
  /* 当前选中的项目（"" ＝ 总账户 / 未归项目）。
     它只决定**新账默认记到哪**；查询口径始终是全部项目（2026-09-26 拍板）。
     起因（欠账 E 组 18）：原先没说项目的账会落到 projects.json 里第一个项目，
     用户既看不出、也改不了 —— 所以左栏必须能选、且选中态要显眼。 */
  const [activeProject, setActiveProject] = useState("");
  /* 确认草稿时逐笔挑的项目（草稿 id → 项目 id/名字）。归属是逐笔属性，不是会话属性：
     这次说本月报销、下一句说三个月后回款，必须在确认那一刻能分开。 */
  const [draftProject, setDraftProject] = useState<Record<string, string>>({});
  // 选中的项目被删掉之后，别让"当前项目"指向一个不存在的 id
  useEffect(() => {
    if (!activeProject || !projView) return;
    if (!projView.projects.some((x) => x.id === activeProject)) setActiveProject("");
  }, [activeProject, projView]);
  // 登录：原型阶段只在本地记一个用户名，不接账号体系；不登录也能用全部功能
  const [user, setUser] = useState("");
  const [loginOpen, setLoginOpen] = useState(false);
  const [nameText, setNameText] = useState("");
  // 导入：拖文件 / 拖整个文件夹（按文件夹名建项目）
  const [dragActive, setDragActive] = useState(false);
  const [intakeBusy, setIntakeBusy] = useState(false);
  // 附件入口：一个「＋」按钮，配三个隐藏选择器（图片 / 文件 / 整个文件夹）
  const [attachOpen, setAttachOpen] = useState(false);
  const imageRef = useRef<HTMLInputElement>(null);
  const dirRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  async function refresh() {
    try {
      // 第 4 个（capabilities）保留为「后端还在吗」的探活调用；LLM 可不可用由下方模型按钮体现，
      // 不再单独显示（原来顶栏那行「N 笔 · 本地账本 · LLM 对话」已被 David 要求去掉）。
      const [s, f, e, , m] = await Promise.all([
        j<{ state: StateT }>("/api/state"),
        j<Fc>("/api/forecast?horizon_days=30"),
        j<{ events: Ev[] }>("/api/events?limit=20"),
        j<{ llm: boolean }>("/api/capabilities"),
        j<ModelsView>("/api/models"),
      ]);
      setState(s.state);
      setFc(f);
      setEvents(e.events);
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
    // 确认时把"这一笔归哪个项目"一起提交（下拉里选的；没动过就是草稿原来的项目）
    const proj = draftProject[p.id] ?? p.project ?? "";
    try {
      const res = await j<{ ok: boolean; project_name?: string }>(
        `/api/pending/${p.id}/${acceptIt ? "accept" : "decline"}`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project: proj }) }
      );
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
            ? `已确认入账 ${yuan(p.amount_cents)}（${p.category}），归入「${res.project_name || "未归项目"}」。`
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

  /** 删除项目：两种口径由用户自己选（只删项目 / 连同账单一起删）。
   *  账本是追加式的，所以「一起删」在后端是追加一条作废记录，不是重写历史。 */
  async function deleteProject(id: string, withData: boolean, name: string) {
    try {
      await j<{ ok: boolean; voided_count: number; bill_count: number }>(
        `/api/projects/${encodeURIComponent(id)}?with_data=${withData ? "true" : "false"}`,
        { method: "DELETE" },
      );
      setProjMsg(withData
        ? `「${name}」与它名下的账单已一起删除。`
        : `「${name}」已删除；它名下的账单还在账本里，回到「未归项目」。`);
      setDeletingId("");
      refresh();
    } catch {
      setProjMsg("删除失败：请确认后端在运行。");
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

  /** 文件选择：图片 / 文件 / 整个文件夹三个选择器共用。
   *  文件夹选择要带上相对路径，这样第一层目录名会成为项目名（与拖拽导入同一口径）。 */
  async function onPickFiles(e: ChangeEvent<HTMLInputElement>) {
    const list = Array.from(e.target.files ?? []);
    e.target.value = "";
    if (!list.length) return;
    await uploadFiles(
      list.map((f) => ({
        name: f.name,
        rel_path: (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name,
        file: f,
      }))
    );
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
  /** 拖拽 / 选择文件后的导入。走流式端点，**逐文件把处理过程按四个阶段写进对话**。
   *  起因（David 2026-09-25）：一次导十几张票要跑几分钟，只在最后回一句结果，
   *  用户分不清是在跑还是卡住；而且结果堆成一坨，看不出每一步做了什么。
   *  进度全部来自后端真实处理结果，不做假进度条。 */
  async function uploadFiles(items: FileItem[]) {
    if (!items.length || intakeBusy) return;
    setIntakeBusy(true);
    setDragActive(false);
    setMsgs((m) => [...m, { role: "user", text: `（导入 ${items.length} 个文件）` }]);

    let blocks: ProgressBlock[] = [];
    const blank = (): IntakeStages => ({ read: "读取中…", recognized: "—", processed: "—", how: "—" });

    /** 原地重画进度气泡：文件块 + 一行当前状态，不刷屏。 */
    const paint = (live: string) => {
      setMsgs((m) => {
        const last = m[m.length - 1];
        const msg: Msg = { role: "ai", text: live, blocks: [...blocks], progress: true };
        return last && last.progress ? [...m.slice(0, -1), msg] : [...m, msg];
      });
    };

    try {
      paint(`正在接收这 ${items.length} 个文件（本机读取，原文不上传）…`);
      const files = await Promise.all(items.map(async (it) => ({
        name: it.name,
        rel_path: it.rel_path,
        content_b64: await fileToB64(it.file),
      })));

      const res = await fetch("/api/intake/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ files }),
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

      let summary = "";
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done: streamEnd } = await reader.read();
        if (streamEnd) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n");
        buf = parts.pop() ?? "";
        for (const raw of parts) {
          if (!raw.trim()) continue;
          const ev = JSON.parse(raw) as IntakeEvent;
          if (ev.stage === "start") {
            blocks = blocks.slice(0, ev.index - 1);
            blocks.push({ file: ev.file, done: false, stages: blank() });
            paint(`正在读文件（${ev.index}/${ev.total}）：${ev.file}`);
          } else if (ev.stage === "tick") {
            /* 心跳：后端还在跑这个文件（真机实测一张报销表要 239 秒）。
               显示**真实已等秒数**与正在做的动作，不显示假百分比；
               它同时让这条流的连接不会长时间静默而被掐断（否则只会看到 network error）。 */
            const secs = ev.elapsed >= 60
              ? `${Math.floor(ev.elapsed / 60)} 分 ${Math.round(ev.elapsed % 60)} 秒`
              : `${Math.round(ev.elapsed)} 秒`;
            blocks[ev.index - 1] = {
              file: ev.file, done: false,
              stages: { read: "读取中…", recognized: "—", processed: "—",
                        how: `${ev.note}（已 ${secs}）` },
            };
            paint(`正在读文件（${ev.index}/${ev.total}）：${ev.file} · 已 ${secs}`);
          } else if (ev.stage === "file_done") {
            blocks[ev.index - 1] = { file: ev.file, done: true, stages: ev.stages ?? blank() };
            paint(ev.index < ev.total
              ? `正在读文件（${ev.index + 1}/${ev.total}）…`
              : "正在收尾：把草稿归到项目里…");
          } else if (ev.stage === "file_failed") {
            blocks[ev.index - 1] = {
              file: ev.file, done: true,
              stages: { read: "—", recognized: "—", processed: "未生成草稿",
                        how: `${ev.error}——已跳过，不影响其他文件` },
            };
            paint("继续处理剩下的文件…");
          } else if (ev.stage === "all_done") {
            summary = summarizeIntake(ev.batches ?? [], false).join("\n");
          }
        }
      }

      setMsgs((m) => {
        const last = m[m.length - 1];
        const msg: Msg = { role: "ai", text: summary || "没有可导入的内容。", blocks: [...blocks] };
        return last && last.progress ? [...m.slice(0, -1), msg] : [...m, msg];
      });
      refresh();
    } catch (err) {
      const text = `导入失败：${err instanceof Error ? err.message : String(err)}`;
      setMsgs((m) => {
        const last = m[m.length - 1];
        const msg: Msg = { role: "ai", text, blocks: [...blocks] };
        return last && last.progress ? [...m.slice(0, -1), msg] : [...m, msg];
      });
    } finally {
      setIntakeBusy(false);
    }
  }

  /** 输入框随内容长高：回车换行之后能看出是多行，最高约 5 行，再高就自己滚。 */
  function autoGrow(el: HTMLTextAreaElement) {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 120)}px`;
  }

  /** 关于弹窗开着时按 Esc 关闭。 */
  useEffect(() => {
    if (!aboutOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setAboutOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [aboutOpen]);

  async function send() {
    const t = text.trim();
    if (!t || busy) return;
    setMsgs((m) => [...m, { role: "user", text: t }]);
    setText("");
    if (inputRef.current) inputRef.current.style.height = "auto"; // 发送后收回一行高
    setBusy(true);
    try {
      const r = await j<ChatReply>("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: t, session_id: sessionId(), project: activeProject }),
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
  const acct = accountTotals(projView);
  const activeProjectName = activeProject === ""
    ? "未归项目（总账户）"
    : (projView?.projects.find((x) => x.id === activeProject)?.name ?? activeProject);

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
            CashLens <span className="brand-sub">本地工作台</span>
          </div>
          <div className="topbar-right">
            {/* 状态标签（「学习中」那个）已按 David 要求从右上角撤掉：孤零零一个词看不明白。
                同样的信息移到左栏「财务状态」卡片里，那里有上下文。
                顶栏只在后端真连不上时提示一句 —— 线上出问题时这是第一线索，不能一并抹掉。 */}
            {!apiOk && (
              <span className="tag t-unknown">
                <span className="dot" />
                后端未连接
              </span>
            )}
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
                <span>图片 / PDF / Excel / CSV 都行；拖整个文件夹就按文件夹名建项目（拖进来不会弹浏览器的上传确认）</span>
              </div>
            )}
            {/* 头部（「钱的事，说给我听」+「N 笔 · 本地账本 · LLM 对话」）已按 David 要求去掉：
                那行元信息与底部模型按钮重复，去掉后对话区顶部多出一整块可用高度。 */}
            <div className="log" ref={logRef}>
              {msgs.map((m, i) => (
                <div key={i} className={`b ${m.role}${m.progress ? " progress" : ""}`}>
                  {m.blocks && m.blocks.length > 0 && (
                    <div className="pb-list">
                      {m.blocks.map((b, j) => (
                        <div className="pb" key={j}>
                          <div className="pb-file">
                            <span className="pb-icon" aria-hidden="true">{b.done ? "📄" : "⏳"}</span>
                            {b.file}
                          </div>
                          {STAGE_LABELS.map(([key, label]) => (
                            <div className="pb-row" key={key}>
                              <span className="pb-k">{label}</span>
                              <span className="pb-v">{b.stages[key] || "—"}</span>
                            </div>
                          ))}
                        </div>
                      ))}
                    </div>
                  )}
                  {m.text}
                  {m.pending && m.pending.length > 0 && (
                    <div className="acts">
                      {m.pending.map((p) => (
                        <div className="act-row" key={p.id}>
                          <span className="num">
                            {p.direction === "income" ? "收入" : "支出"} {yuan(p.amount_cents)}（{p.category}）
                          </span>
                          {/* 归属在确认那一刻可改：草稿上的项目是识别时定下的，而用户不一定说对 */}
                          <select
                            className="proj-pick"
                            value={draftProject[p.id] ?? p.project ?? ""}
                            onChange={(e) => setDraftProject((s) => ({ ...s, [p.id]: e.target.value }))}
                            aria-label="这笔归入哪个项目"
                            disabled={actingIds.has(p.id)}
                          >
                            <option value="">未归项目（总账户）</option>
                            {(projView?.projects ?? []).map((x) => (
                              <option key={x.id} value={x.id}>{x.name}</option>
                            ))}
                          </select>
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
            {/* 输入区上方常显"正在记入哪本账"，免得用户不知道新账会落到哪儿 */}
            <div className="scope-bar">
              正在记入：<b>{activeProjectName}</b>
              <span className="sb-note">查询始终是全部项目合计（总账户）</span>
            </div>
            <div className="inputrow">
              <div className="attach">
                <span className="tip">
                  <button
                    type="button"
                    className="btn-plus"
                    onClick={() => setAttachOpen((v) => !v)}
                    disabled={!apiOk || intakeBusy}
                    aria-haspopup="menu"
                    aria-expanded={attachOpen}
                    aria-label="添加附件"
                  >
                    {intakeBusy ? "…" : "＋"}
                  </button>
                  <span className="tip-text">添加图片、票据文件，或整个文件夹</span>
                </span>
                {attachOpen && (
                  <div className="attach-menu" role="menu">
                    <button type="button" role="menuitem" onClick={() => { setAttachOpen(false); imageRef.current?.click(); }}>
                      图片或截图
                      <span className="am-sub">发票、小票、微信与支付宝截图</span>
                    </button>
                    <button type="button" role="menuitem" onClick={() => { setAttachOpen(false); fileRef.current?.click(); }}>
                      发票与表格文件
                      <span className="am-sub">数电发票 PDF、项目工时 Excel、账单 CSV</span>
                    </button>
                    <button type="button" role="menuitem" onClick={() => { setAttachOpen(false); dirRef.current?.click(); }}>
                      整个文件夹
                      <span className="am-sub">按文件夹名自动建项目，里面有多少张都收</span>
                      {/* 浏览器对「上传整个目录」有强制确认框，页面关不掉（安全设置，
                          页面不该能偷偷枚举你的目录）。所以如实说清，并给出免确认的另一条路。 */}
                      <span className="am-sub">
                        浏览器会问一次「上传此文件夹下的所有文件？」，点「上传」即可；不想多这一步就把文件夹直接拖进对话区
                      </span>
                    </button>
                  </div>
                )}
              </div>
              <textarea
                ref={inputRef}
                value={text}
                rows={1}
                onChange={(e) => {
                  setText(e.target.value);
                  autoGrow(e.target);
                }}
                onKeyDown={(e) => {
                  // 回车 = 换行（原先是回车直接发送，话没写完就被发出去了）。
                  // 发送走右下角「发送」按钮，或用 Ctrl / Cmd + 回车。
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                    e.preventDefault();
                    send();
                  }
                }}
                placeholder='记账或问现金流，如「打车花了 28」（回车换行，Ctrl / Cmd + 回车发送）'
                aria-label="输入一句话，回车换行，Ctrl 加回车发送"
                disabled={!apiOk}
              />
              <div className="modelrow">
                <span className="tip">
                  <button
                    className="model-pick"
                    onClick={() => setPickOpen((v) => !v)}
                    disabled={!apiOk || modelBusy}
                    aria-haspopup="listbox"
                    aria-expanded={pickOpen}
                    aria-label="选择模型"
                  >
                    <span
                      className={`dot ${
                        models?.tier === "cloud" ? "cloud" : models?.tier === "none" ? "off" : ""
                      }`}
                    />
                    {modelBusy ? "切换中…" : currentModelLabel()}
                    <span className="caret" aria-hidden="true">▾</span>
                  </button>
                  <span className="tip-text">
                    模型{models?.active.model ? `：${models.active.model}` : ""}
                  </span>
                </span>
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
              <button
                type="button"
                className="btn btn-send"
                onClick={send}
                disabled={!apiOk || busy}
              >
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
            </div>
            {modelMsg && <div className="hint">{modelMsg}</div>}
            {/* 三个隐藏的文件选择器：图片 / 文件 / 整个文件夹，都由左侧「＋」触发 */}
            <input ref={imageRef} type="file" hidden multiple accept="image/*" onChange={onPickFiles} />
            <input
              ref={fileRef}
              type="file"
              hidden
              multiple
              accept=".jpg,.jpeg,.png,.webp,.gif,.pdf,.xlsx,.xlsm,.csv"
              onChange={onPickFiles}
            />
            <input
              ref={dirRef}
              type="file"
              hidden
              multiple
              {...({ webkitdirectory: "true" } as Record<string, string>)}
              onChange={onPickFiles}
            />
          </section>

          {/* 右侧面板 */}
          <aside className="card side" aria-label="现金流面板">
            {/* 项目维度：账本按项目归集，这里是项目的唯一入口 */}
            <section className="sect">
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
                /* 浮层：新建表单不占文档流，左栏高度不受影响（否则点开就变高） */
                <div className="proj-pop new">
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
                </div>
              )}
              {/* 当前项目（欠账 E 组 18）：选中＝新账默认记到这里，选中态高亮。
                  它只决定"新账记到哪"；查询始终是全部项目合计（见输入区那行提示）。 */}
              <button
                type="button"
                className={`proj-sel${activeProject === "" ? " on" : ""}`}
                onClick={() => setActiveProject("")}
                aria-pressed={activeProject === ""}
                title="选中它：新账不指定具体项目，进「未归项目」；查询本来就是全部项目合计"
              >
                <span className="ps-name">总账户（全部项目）</span>
                <span className="ps-sub">
                  支出 {yuan(acct.expense)} · 收入 {yuan(acct.income)} · {acct.count} 笔
                </span>
              </button>
              <div className="proj-tip">点一个项目＝新账默认记到它；每笔在确认时还能单独改</div>
              {!projView || (projView.project_count === 0 && projView.unassigned.count === 0) ? (
                <div className="empty">还没有项目。点上面的「新建项目」，或记账时说一句「这笔记到 919 昆明项目」，就建好了</div>
              ) : (
                <>
                  {projView.projects.map((p) => (
                    <div key={p.id} className="proj-item">
                      <div className={`ev${activeProject === p.id ? " on" : ""}`}>
                        <button
                          type="button"
                          className="ev-pick"
                          onClick={() => setActiveProject(p.id)}
                          aria-pressed={activeProject === p.id}
                          title="选它＝新账默认记到这个项目"
                        >
                          <b>{p.name}</b>
                          <span className="m">
                            支出 {yuan(p.expense_cents)} · 收入 {yuan(p.income_cents)} · {p.count} 笔
                          </span>
                          {!p.named && <span className="m">默认名，建议改成这个项目的完整名称</span>}
                        </button>
                        <div className="ev-acts">
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
                          <button
                            className="btn-x"
                            title="删除项目"
                            aria-label={`删除项目「${p.name}」`}
                            aria-expanded={deletingId === p.id}
                            onClick={() => {
                              setDeletingId(deletingId === p.id ? "" : p.id);
                              setRenamingId("");
                              setProjMsg("");
                            }}
                          >
                            ×
                          </button>
                        </div>
                      </div>
                      {deletingId === p.id && (
                        /* 浮层：删除选择块不占文档流（原先会把左栏顶高一大截） */
                        <div className="proj-pop row">
                          <div className="proj-del">
                            <div className="pd-title">
                              删除「{p.name}」？
                              {p.count > 0 ? `它名下已有 ${p.count} 笔账单。` : "它名下还没有账单。"}
                            </div>
                            <div className="pd-opt">
                              <button className="btn-mini" onClick={() => deleteProject(p.id, false, p.name)}>
                                {p.count > 0 ? "只删项目" : "删除项目"}
                              </button>
                              <span>
                                {p.count > 0
                                  ? `账本记录不动，这 ${p.count} 笔回到「未归项目」，数字不会消失。`
                                  : "账本里没有它的记录，删掉即可。"}
                              </span>
                            </div>
                            {p.count > 0 && (
                              <div className="pd-opt">
                                <button className="btn-mini danger" onClick={() => deleteProject(p.id, true, p.name)}>
                                  连账单一起删
                                </button>
                                <span>这 {p.count} 笔同时作废，界面与统计都不再计入。</span>
                              </div>
                            )}
                            <div className="pd-opt">
                              <button className="btn-mini" onClick={() => setDeletingId("")}>取消</button>
                            </div>
                          </div>
                        </div>
                      )}
                      {renamingId === p.id && (
                        /* 浮层：改名表单不占文档流，位置就贴着这一行 */
                        <div className="proj-pop row">
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

            <section className="sect">
              <div className="sect-head">
                <h3>财务状态（状态引擎）</h3>
                <span
                  className={`tag ${state ? `t-${state.label}` : "t-unknown"}`}
                  title="状态引擎按证据强度算出来的当前状态标签；可信度低时会如实说「现金流不明」"
                >
                  <span className="dot" />
                  {state ? LABEL_CN[state.label] ?? state.label : apiOk ? "载入中…" : "后端未连接"}
                </span>
              </div>
              <div className="kpi">
                <div className="cell">
                  <div className="v num">{healthPct}%</div>
                  <div className="k">
                    健康度
                    <Hint text="按你记的每一笔逐笔更新：收入把它推高、支出把它压低，证据越硬权重越大。它不是资产数字，是账本证据算出来的状态分。" />
                  </div>
                </div>
                <div className="cell">
                  <div className="v num">{confPct}%</div>
                  <div className="k">
                    现金流可信度
                    <Hint text="看最近一笔收入距今多久、收入间隔稳不稳。越久没进账、间隔越乱，这个数字越低；数据太少时它会很低，我们就直接说「现金流不明」，不编数字。" />
                  </div>
                </div>
              </div>
              <div className="bar good">
                <i style={{ width: `${healthPct}%` }} />
              </div>
              <div className="bar">
                <i style={{ width: `${confPct}%` }} />
              </div>
            </section>

            <section className="sect">
              <div className="sect-head">
                <h3>
                  {fc && fc.extrapolated === false
                    ? "现金流预测（数据不足，暂不外推）"
                    : "未来 30 天现金流（90% 区间）"}
                  <Hint text="按你过去 90 天的收支节奏往后推 30 天。区间下沿是偏悲观的情形、上沿是偏乐观的情形，真实结果大约有 90% 的可能性落在这两条线之间。" />
                </h3>
              </div>
              {fc ? (
                fc.extrapolated === false ? (
                  <>
                    <div className="fc-row">
                      <span className="k">
                        当前已知净额
                        <Hint text="账本里所有收入减掉所有支出之后的数。数据还不够 5 天，我们不硬推 30 天，只把已经记下的实情告诉你。" />
                      </span>
                      <b className={`num ${fc.median_balance_cents < 0 ? "amt-out" : "amt-in"}`}>
                        {yuan(fc.median_balance_cents)}
                      </b>
                    </div>
                    <div className="fc-note">{fc.reason}</div>
                  </>
                ) : (
                  <>
                    <div className="fc-row">
                      <span className="k">
                        期末预计（中位）
                        <Hint text="30 天后最可能落在的位置：一半概率高于它、一半低于它。" />
                      </span>
                      <b className="num">{yuan(fc.median_balance_cents)}</b>
                    </div>
                    <div className="fc-row">
                      <span className="k">
                        区间下沿
                        <Hint text="偏悲观的情形：90% 的可能，30 天后的余额不会低于这个数。" />
                      </span>
                      <b className={`num ${fc.band90_low_cents < 0 ? "amt-out" : "amt-in"}`}>{yuan(fc.band90_low_cents)}</b>
                    </div>
                    <div className="fc-row">
                      <span className="k">
                        区间上沿
                        <Hint text="偏乐观的情形：90% 的可能，30 天后的余额不会高于这个数。" />
                      </span>
                      <b className="num">{yuan(fc.band90_high_cents)}</b>
                    </div>
                    <div className="fc-note">{fc.reason} · 可信度低时如实标注「现金流不明」</div>
                  </>
                )
              ) : (
                <div className="empty">{apiOk ? "等待数据…" : "后端未连接"}</div>
              )}
            </section>

            <section className="sect">
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
        <p className="footnote">
          CashLens · 本地优先 · 数据留在磁盘 · 证据驱动（R38 纪律：此处每个数字都来自真实计算）
          <button className="link-about" onClick={() => setAboutOpen(true)}>关于</button>
        </p>
      </div>

      {aboutOpen && (
        <div
          className="modal-mask"
          role="dialog"
          aria-modal="true"
          aria-label="关于 CashLens"
          onClick={() => setAboutOpen(false)}
        >
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <b>关于 CashLens</b>
              <button className="btn-x" title="关闭" aria-label="关闭" onClick={() => setAboutOpen(false)}>
                ×
              </button>
            </div>
            <dl className="about-list">
              <div>
                <dt>版本</dt>
                <dd>0.1.0 · 原型 / 早期验证阶段</dd>
              </div>
              <div>
                <dt>产品</dt>
                <dd>经营现金流可视化与决策辅助工具。一个人就是一家公司——让经营现金流看得清、撑得住。</dd>
              </div>
              <div>
                <dt>能做什么</dt>
                <dd>票据与账单导入（图片 / PDF / Excel 报销表 / CSV）、财务状态引擎、现金流区间预测；接单报价决策开发中。</dd>
              </div>
              <div>
                <dt>开发者</dt>
                <dd>David · 个人开发者</dd>
              </div>
              <div>
                <dt>联系方式</dt>
                <dd>davidlee_2026@sina.com</dd>
              </div>
              <div>
                <dt>数据</dt>
                <dd>本地优先：账本与预估存在本机 data/ 目录，默认不上云；识别结果一律先经你确认才入账。</dd>
              </div>
            </dl>
            <p className="about-note">只做事实陈述与依据呈现，不构成记账、税务或投资意见。</p>
          </div>
        </div>
      )}
    </>
  );
}
