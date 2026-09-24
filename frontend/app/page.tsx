"use client";

import { useEffect, useRef, useState } from "react";

type PendingDraft = { id: string; direction: "income" | "expense"; amount_cents: number; category: string; channel: string };
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

/** 会话 id：localStorage 持久化，多轮追问共享同一上下文。 */
function sessionId(): string {
  if (typeof window === "undefined") return "";
  let s = window.localStorage.getItem("cl_session");
  if (!s) {
    s = window.crypto?.randomUUID?.() ?? `s-${Date.now()}`;
    window.localStorage.setItem("cl_session", s);
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
    // 挂载后再从 localStorage 恢复历史（与服务端渲染解耦，修复 hydration mismatch）
    if (typeof window === "undefined") return;
    try {
      const raw = window.localStorage.getItem("cl_msgs");
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
          "cl_msgs",
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
        { role: "ai", text: acceptIt ? `已确认入账 ${yuan(p.amount_cents)}（${p.category}）。` : "已忽略，这笔不入账。" },
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
        window.localStorage.setItem("cl_session", r.session_id);
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
          <span className={`tag ${state ? `t-${state.label}` : "t-unknown"}`}>
            <span className="dot" />
            {state ? LABEL_CN[state.label] ?? state.label : apiOk ? "载入中…" : "后端未连接"}
          </span>
        </div>
      </header>

      <div className="main">
        <div className="grid">
          {/* 对话区 */}
          <section className="card convo" aria-label="对话记账">
            <div className="convo-head">
              <span className="t">说一句，钱就记下了</span>
              <span className="muted">
                {state ? `${state.events_count} 笔 · 本地账本 · ` : ""}
                {llmOn ? "LLM 对话" : "规则层"}
              </span>
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
