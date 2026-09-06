"use client";

import { useEffect, useRef, useState } from "react";

type Msg = { role: "user" | "ai"; text: string };
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
type ChatReply = { ok: boolean; text: string; session_id?: string };

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
  const [msgs, setMsgs] = useState<Msg[]>([
    { role: "ai", text: "你好，我是 CashLens 的本地工作台。记账直接说（如「昨天微信收了 3000 尾款」），或问我「下个月现金流怎么样」；接入 LLM 后也能自由聊天。所有金额/状态都真写进本地事件账本并由状态引擎计算，不做剧本。\n\n先记几笔再问我现金流，面板会实时变化。\n（需先启动后端：cd backend && python3 -m uvicorn app.main:app --port 8001）" },
  ]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState<StateT | null>(null);
  const [fc, setFc] = useState<Fc | null>(null);
  const [events, setEvents] = useState<Ev[]>([]);
  const [apiOk, setApiOk] = useState(true);
  const [llmOn, setLlmOn] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  async function refresh() {
    try {
      const [s, f, e, c] = await Promise.all([
        j<{ state: StateT }>("/api/state"),
        j<Fc>("/api/forecast?horizon_days=30"),
        j<{ events: Ev[] }>("/api/events?limit=20"),
        j<{ llm: boolean }>("/api/capabilities"),
      ]);
      setState(s.state);
      setFc(f);
      setEvents(e.events);
      setLlmOn(!!c.llm);
      setApiOk(true);
    } catch {
      setApiOk(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [msgs]);

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
      setMsgs((m) => [...m, { role: "ai", text: r.text }]);
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
            <div className="hint">LLM 语义理解（后端已配 Key 时）+ 规则兜底 → 事件账本 → 状态引擎真计算；金额与状态均为本地真实数据。自由对话文本会上云（见隐私口径）。</div>
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
