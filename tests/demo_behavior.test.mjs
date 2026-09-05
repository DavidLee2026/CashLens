/**
 * CashLens demo（docs/index.html）行为回归测试
 *
 * 背景：页面 demo 的意图分流/金额解析/语音输入逻辑在真实 DOM 里难以单测，
 * 用最小 DOM 桩加载页面真实 <script>，模拟「输入 → 点发送 → 读最后一条 AI 气泡」断言。
 * 运行：node tests/demo_behavior.test.mjs   （退出码 0 = 全过）
 * 基线：9/5 多轮实弹修复累积的 40+ 用例压缩版；新增用例先把 bug 复现，修好再固化为基线。
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const html = readFileSync(path.join(__dirname, "..", "docs", "index.html"), "utf8");
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];

class El {
  constructor() {
    this.children = []; this.handlers = {}; this.value = ""; this.textContent = "";
    this.innerHTML = ""; this.className = ""; this.disabled = false; this.dataset = {};
    this.style = {}; this.scrollTop = 0; this.title = "";
    this.classList = { add() {}, remove() {} };
  }
  addEventListener(t, f) { (this.handlers[t] = this.handlers[t] || []).push(f); return this; }
  click() { (this.handlers.click || []).forEach((f) => f.call(this)); }
  appendChild(c) { this.children.push(c); return c; }
  setAttribute() {} focus() {}
}

const els = {};
const tabs = ["opc", "smallbiz", "family"].map((k) => { const e = new El(); e.dataset.scene = k; return e; });
globalThis.window = globalThis;
globalThis.window.SpeechRecognition = undefined;
globalThis.window.webkitSpeechRecognition = undefined;
globalThis.location = { protocol: "http:", hostname: "127.0.0.1" };
globalThis.document = {
  getElementById: (id) => (els[id] = els[id] || new El()),
  createElement: () => new El(),
  querySelectorAll: (sel) => (sel.indexOf(".demo-tab") > -1 ? tabs : []),
  querySelector: () => null,
};

(0, eval)(script);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const lastAI = () =>
  els.chatLog.children.filter((c) => (c.className || "").indexOf("bubble ai") > -1).slice(-1)[0];

async function ask(text) {
  els.inputText.value = text;
  els.btnSend.click();
  await sleep(700);
  const c = lastAI();
  return c ? c.innerHTML : "";
}

const checks = [];
const check = (name, ok, detail = "") => {
  checks.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok ? "" : "  → " + detail.slice(0, 90)}`);
};

const cases = [
  // —— 9/5 实弹修复基线 ——
  ["嗯，我客户这边二九九套餐愿意支付，呃，有一个客户，然后你记录一下吧。呃，客户支付的是支付宝。",
    (g) => g.includes("¥299.00") && g.includes("收入") && g.includes("支付宝") && !g.includes("支出"), "实弹：299·收入·支付宝"],
  ["昨天微信收了 3000 尾款", (g) => g.includes("¥3,000.00") && g.includes("收入"), "剧本句收入"],
  ["昨天微信收了三千元尾款", (g) => g.includes("¥3,000.00") && g.includes("收入"), "中文金额剧本句"],
  ["我下个月的现金流怎么样呀？我想知道。", (g) => g.includes("CashPulse"), "剧本问题命中"],
  ["来了个设计单，5000 块，2 周交付，接不接？", (g) => g.includes("接单决策引擎"), "接单决策命中"],
  ["我付了三千给客户", (g) => g.includes("¥3,000.00") && g.includes("支出"), "付三千=支出"],
  ["打车花了 28", (g) => g.includes("¥28.00"), "阿拉伯金额"],
  ["咖啡三十八", (g) => g.includes("¥38.00"), "口语直报（句尾）"],
  ["今天是和十个客户接触了，呃，但是他们都没有想要进一步沟通的想法。", (g) => g.includes("接不上"), "十个客户不记账"],
  ["接触了10个客户", (g) => g.includes("接不上"), "10个客户不记账"],
  ["花了10分钟到公司", (g) => g.includes("接不上"), "10分钟不记账"],
  ["十点开会别迟到", (g) => g.includes("接不上"), "十点不记账"],
  ["周三给朋友转账", (g) => g.includes("接不上"), "周三不记账"],
];

(async () => {
  // 模式切换（微信式：默认语音 / 点左键切键盘 / 再点回语音）
  check("默认语音态 holdZone 可见", els.holdZone.style.display === "flex");
  els.btnMode.click();
  check("切键盘后 input+发送 可见", els.inputText.style.display === "" && els.btnSend.style.display === "");
  els.btnMode.click();
  check("切回语音后 holdZone 可见", els.holdZone.style.display === "flex");

  for (const [text, ok, tag] of cases) {
    const got = await ask(text);
    check(tag, ok(got), got);
  }

  const fails = checks.filter((c) => !c.ok).length;
  console.log(`\n${checks.length - fails}/${checks.length} 通过`);
  process.exit(fails ? 1 : 0);
})();
