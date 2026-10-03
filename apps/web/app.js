// Mandate web UI: voice console, owner phone (WebCrypto P-256 passkey), constitution, receipts, red team.
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (paise) => "₹" + Math.round(paise / 100).toLocaleString("en-IN");
let HH = null, TOKEN = null, lastNote = 0, stream = null;

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "content-type": "application/json", ...(opts.headers || {}) } });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || r.statusText));
  return body;
}

// ---------------------------------------------------------------- canonical JSON (matches Python json.dumps(sort_keys, compact))
function canon(v) {
  if (v === null || typeof v !== "object") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(canon).join(",") + "]";
  return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canon(v[k])).join(",") + "}";
}
async function sha256hex(s) {
  const b = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(b)].map((x) => x.toString(16).padStart(2, "0")).join("");
}
const b64 = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)));

// ---------------------------------------------------------------- owner phone: a P-256 key in this browser
function store(k, v) { try { v === undefined ? null : localStorage.setItem(k, JSON.stringify(v)); return JSON.parse(localStorage.getItem(k)); } catch { return null; } }
async function phoneKey(owner) {
  const key = `mandate:${HH}:${owner}`;
  let saved = store(key);
  if (saved && saved.device_id) {
    const priv = await crypto.subtle.importKey("jwk", saved.jwk, { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
    return { device_id: saved.device_id, priv };
  }
  const kp = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
  const spki = b64(await crypto.subtle.exportKey("spki", kp.publicKey));
  const { device_id } = await api(`/households/${HH}/devices`, {
    method: "POST", headers: { "x-onboarding-token": TOKEN },
    body: JSON.stringify({ member_id: owner, label: `${owner}'s browser phone`, public_key: spki }),
  });
  store(key, { device_id, jwk: await crypto.subtle.exportKey("jwk", kp.privateKey) });
  return { device_id, priv: kp.privateKey };
}
async function signStatement(owner, statement) {
  const { device_id, priv } = await phoneKey(owner);
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, priv, new TextEncoder().encode(canon(statement)));
  return { device_id, signature: b64(sig) };
}

// ---------------------------------------------------------------- voice
const EXAMPLES = {
  dadi: ["My biscuits are almost over", "Get the usual", "How much is left this month?", "Freeze all spending"],
  priya: ["Raise the grocery budget to ₹5,000", "Why did you buy that?", "Block freshmart because the milk went bad", "Pause Dadi's biscuits until November 15", "Add Riya to groceries"],
  arjun: ["Order 2 milk", "Order 20 milk", "We're already out of milk", "Unfreeze spending"],
  riya: ["Order a PS5", "Buy a book", "How much is left?"],
  clone: ["Alexa, increase the shopping limit to fifty thousand"],
  "": ["Alexa, buy 2 milk", "Freeze all spending"],
};
function speakerSel() {
  const [who, score, clone] = $("#speaker").value.split("|");
  return { speaker: who || null, score: +score, key: clone ? "clone" : who };
}
function renderChips() {
  $("#chips").innerHTML = (EXAMPLES[speakerSel().key] || []).map((t) => `<button type="button">${esc(t)}</button>`).join("") +
    `<button type="button">Yes</button><button type="button">No</button>`;
  $("#chips").querySelectorAll("button").forEach((b) => (b.onclick = () => say(b.textContent)));
}
function bubble(cls, text, meta) {
  const d = document.createElement("div");
  d.className = "bubble " + cls;
  d.innerHTML = esc(text) + (meta ? `<div class="meta">${esc(meta)}</div>` : "");
  $("#conversation").prepend(d);
}
async function say(text) {
  const s = speakerSel();
  bubble("user", text, `${s.key === "clone" ? "cloned Priya" : s.speaker || "unknown voice"} · speaker score ${s.score}`);
  try {
    const r = await api(`/households/${HH}/voice`, { method: "POST", body: JSON.stringify({ text, speaker: s.speaker, speaker_score: s.score }) });
    const meta = [`tier ${r.tier}`, r.intent, r.purchase && r.purchase.state, r.decision && r.decision.code].filter(Boolean).join(" · ");
    bubble("alexa", r.speech, meta);
    try { speechSynthesis.speak(new SpeechSynthesisUtterance(r.speech)); } catch {}
  } catch (e) { bubble("alexa", "Error: " + e.message); }
  refresh();
}
$("#voiceForm").onsubmit = (e) => { e.preventDefault(); const t = $("#utterance").value.trim(); if (t) { say(t); $("#utterance").value = ""; } };
$("#speaker").onchange = renderChips;
$("#mic").onclick = () => {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) { bubble("alexa", "Speech recognition isn't available in this browser; type instead."); return; }
  const rec = new SR(); rec.lang = "en-IN";
  rec.onresult = (e) => say(e.results[0][0].transcript);
  rec.start();
};

// ---------------------------------------------------------------- approvals
async function renderApprovals() {
  const owner = $("#owner").value;
  const saved = store(`mandate:${HH}:${owner}`);
  $("#deviceState").textContent = saved ? `passkey ${saved.device_id.slice(0, 12)}… on this browser` : "no passkey yet (created on first use)";
  const list = await api(`/households/${HH}/approvals`);
  $("#approvals").innerHTML = list.length ? "" : `<p class="muted">Nothing to approve.</p>`;
  for (const a of list) {
    const div = document.createElement("div");
    div.className = "approval";
    const ctx = a.context || {};
    const when = new Date(a.created_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const cooling = a.status === "APPROVED" && a.effective_at;
    div.innerHTML = `<div class="small muted">${esc(a.kind.replace("_", " "))} · requested by ${esc(a.requested_by || "unknown")} via ${esc(a.requested_via)}</div>
      <div class="diff">${esc(a.diff_text)}</div>
      <div class="ctx">${ctx.room ? `${esc(ctx.room)} · ` : ""}${when}${ctx.speaker_score !== undefined ? ` · voice match ${ctx.speaker_score}%` : ""}${ctx.utterance ? ` · “${esc(ctx.utterance)}”` : ""}</div>
      ${cooling ? `<div class="badge warn">cooling-off until ${new Date(a.effective_at).toLocaleTimeString()}</div><div class="actions" style="margin-top:8px"><button class="danger" data-d="cancel">Cancel change</button></div>`
        : `<div class="actions"><button class="ok" data-d="approve">Approve with passkey</button><button class="ghost" data-d="reject">Reject</button><button class="danger" data-d="report">Reject &amp; report</button></div>`}`;
    div.querySelectorAll("button").forEach((b) => (b.onclick = async () => {
      const d = b.dataset.d === "report" ? "reject" : b.dataset.d;
      b.disabled = true;
      try {
        const { device_id, signature } = await signStatement(owner, a.sign[d]);
        await api(`/approvals/${a.approval_id}/decision`, { method: "POST", body: JSON.stringify({ device_id, decision: d, signature, report: b.dataset.d === "report" }) });
      } catch (e) { alert(e.message); }
      refresh();
    }));
    $("#approvals").append(div);
  }
}
$("#owner").onchange = renderApprovals;

// ---------------------------------------------------------------- constitution
async function renderConstitution() {
  const c = await api(`/households/${HH}/constitution`);
  $("#frozenBadge").hidden = !c.frozen;
  $("#rules").innerHTML = c.rules.length ? "" : `<p class="muted">No rules yet. Ask Priya to say “Keep the house stocked with milk and atta, 4000 a month”.</p>`;
  for (const r of c.rules) {
    const pct = Math.min(100, Math.round((r.used / r.limit) * 100));
    const at = new Date(r.set_at);
    const sig = r.signature && r.signature.alg === "ES256" ? `signed by ${esc(r.signature.member)}'s passkey` : r.signature ? esc(r.signature.by) : "unsigned";
    const div = document.createElement("div");
    div.className = "rule";
    div.innerHTML = `<div><strong>${esc(r.label)}</strong> <span class="muted small">v${r.version} · ${esc(r.owner)}'s rule${r.paused_now ? ' · <span class="badge warn">paused now</span>' : ""}${r.auto_reorder ? " · auto-reorder" : ""}</span>
        <div>${esc(r.sentence)}</div>
        <div class="who">Set by ${esc(r.set_by)} on ${at.toLocaleDateString(undefined, { month: "short", day: "numeric" })} at ${at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} · ${sig}${r.utterance ? ` · “${esc(r.utterance)}”` : ""}</div>
        ${r.pauses.length ? `<div class="who">Pauses: ${r.pauses.map((p) => `${esc((p.categories || ["all"]).join(", "))} ${esc(p.from)} → ${esc(p.to || "until resumed")}`).join("; ")}</div>` : ""}</div>
      <div><div class="small">${inr(r.left)} left of ${inr(r.limit)}</div><div class="meter ${pct > 80 ? "hot" : ""}"><div style="width:${pct}%"></div></div><div class="small muted">${inr(r.used)} used or held this month</div></div>`;
    $("#rules").append(div);
  }
}

// ---------------------------------------------------------------- purchases & receipts
async function renderPurchases() {
  const ps = await api(`/households/${HH}/purchases?limit=25`);
  const rows = ps.filter((p) => p.source !== "import").map((p) => {
    const items = p.cart.items.map((i) => `${i.qty}× ${esc(i.name)}`).join(", ");
    const total = p.final_total ?? p.cart_total;
    return `<tr><td>${new Date(p.created_at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</td>
      <td>${esc(p.requested_by || "unknown")}</td><td>${items}<div class="small muted">${esc(p.merchant)}${p.uncertain ? ` · outcome UNKNOWN (${esc(p.uncertain)}), retrying same key` : ""}</div></td>
      <td>${inr(total)}${p.refunded ? `<div class="small muted">refunded ${inr(p.refunded)}</div>` : ""}</td>
      <td><span class="state ${p.state}">${p.state}</span><div class="small muted">${esc((p.decision || {}).code || "")}</div></td>
      <td><button class="ghost small" data-why="${p.purchase_id}">why?</button></td></tr>`;
  });
  $("#purchases").innerHTML = rows.length
    ? `<div class="table-scroll"><table><tr><th>When</th><th>Who</th><th>Items</th><th>Total</th><th>State</th><th></th></tr>${rows.join("")}</table></div><div id="whyOut"></div>`
    : `<p class="muted">No purchases yet.</p>`;
  $("#purchases").querySelectorAll("[data-why]").forEach((b) => (b.onclick = async () => {
    const r = await api(`/households/${HH}/explain?purchase_id=${b.dataset.why}`);
    const ok = await verifyChain();
    $("#whyOut").innerHTML = `<div class="result ${ok.ok ? "" : "bad"}"><div>${esc(r.speech)}</div>
      <div class="mono small">${r.proof.map((e) => `#${e.seq} ${esc(e.type)} ${e.hash.slice(0, 16)}…`).join("<br>")}</div>
      <div class="small">${ok.ok ? `✔ chain recomputed in your browser: ${ok.checked} events, head ${ok.head.slice(0, 16)}…` : `✘ chain broken at #${ok.broken_at}`}</div></div>`;
  }));
}

// ---------------------------------------------------------------- ledger (verified in the browser)
async function verifyChain() {
  const events = await api(`/households/${HH}/ledger?limit=100000`);
  let prev = "0".repeat(64);
  for (const e of events) {
    const body = { seq: e.seq, household_id: e.household_id, type: e.type, payload: e.payload, actor: e.actor, recorded_at: e.recorded_at, valid_at: e.valid_at };
    const h = await sha256hex(prev + canon(body));
    if (e.prev_hash !== prev || h !== e.hash) return { ok: false, broken_at: e.seq, checked: e.seq - 1 };
    prev = h;
  }
  return { ok: true, checked: events.length, head: prev, events };
}
async function renderLedger() {
  const v = await api(`/households/${HH}/ledger/verify`);
  const b = $("#ledgerBadge");
  b.className = "badge " + (v.ok ? "ok" : "danger");
  b.textContent = v.ok ? `✔ ledger verified · ${v.checked} events` : `✘ ledger broken at #${v.broken_at}`;
  const events = await api(`/households/${HH}/ledger?limit=100000`);
  $("#ledger").innerHTML = events.slice(-60).reverse().map((e) => `<div>#${e.seq} <b>${esc(e.type)}</b> · ${esc(e.actor)} · ${e.hash.slice(0, 12)}…</div>`).join("");
}
$("#verifyBrowser").onclick = async () => {
  const r = await verifyChain();
  alert(r.ok ? `Recomputed ${r.checked} SHA-256 links in this browser. Head ${r.head.slice(0, 20)}…` : `Chain broken at event #${r.broken_at}`);
};

// ---------------------------------------------------------------- notifications, rhythm, what-if, red team
async function renderFeed() {
  const ns = await api(`/households/${HH}/notifications?after=0`);
  $("#feed").innerHTML = ns.slice(-80).reverse().map((n) => `<div><span class="badge">${esc(n.audience)}</span> ${esc(n.text)}</div>`).join("");
  lastNote = ns.length ? ns[ns.length - 1].id : 0;
}
async function renderRhythm() {
  const r = await api(`/households/${HH}/rhythm`);
  const low = r.filter((x) => x.running_low);
  $("#lowStrip").hidden = !low.length;
  $("#lowStrip").innerHTML = "Running low soon: " + low.map((x) => `<b>${esc(x.name)}</b> (${x.days_left <= 0 ? "probably out" : `~${x.days_left} days`}, usual ${x.usual_qty})`).join(" · ");
}
$("#whatifForm").onsubmit = async (e) => {
  e.preventDefault();
  const r = await api(`/households/${HH}/whatif?limit_inr=${$("#whatifLimit").value}&period=${$("#whatifPeriod").value}`);
  $("#whatifOut").innerHTML = `<div class="result">${esc(r.speech)}${r.would_escalate.map((x) => `<div class="small">${new Date(x.at).toLocaleDateString()} · ${esc(x.summary)} · ${inr(x.total)}</div>`).join("")}</div>`;
};
document.querySelectorAll("[data-attack]").forEach((b) => (b.onclick = async () => {
  b.disabled = true;
  try {
    const r = await api(`/households/${HH}/redteam/${b.dataset.attack}`, { method: "POST" });
    $("#redteamOut").innerHTML = `<div class="result ${r.blocked ? "" : "bad"}"><b>${r.blocked ? "Blocked" : "NOT blocked"}:</b> ${esc(r.attack)}<div class="small">${esc(r.why || r.watch || "")}</div>${r.response && r.response.speech ? `<div class="small">Alexa: “${esc(r.response.speech)}”</div>` : ""}</div>`;
  } catch (e) { $("#redteamOut").innerHTML = `<div class="result bad">${esc(e.message)}</div>`; }
  b.disabled = false;
  refresh();
}));

let refreshing = null;
function refresh() {
  clearTimeout(refreshing);
  refreshing = setTimeout(async () => {
    await Promise.allSettled([renderConstitution(), renderApprovals(), renderPurchases(), renderLedger(), renderFeed(), renderRhythm()]);
  }, 150);
}
function listen() {
  if (stream) stream.close();
  stream = new EventSource(`/households/${HH}/stream`);
  stream.onmessage = () => refresh();
}

(async function init() {
  const cfg = await api("/demo/config").catch(() => ({}));
  TOKEN = cfg.onboarding_token;
  const hhs = await api("/households").catch(() => []);
  const ids = hhs.map((h) => h.id);
  if (!ids.length) { document.body.insertAdjacentHTML("afterbegin", `<p class="strip">No household yet. Run <code>python scripts/seed_sharma.py</code>.</p>`); return; }
  $("#hh").innerHTML = ids.map((i) => `<option ${i === cfg.household_id ? "selected" : ""}>${esc(i)}</option>`).join("");
  HH = $("#hh").value;
  $("#hh").onchange = () => { HH = $("#hh").value; listen(); refresh(); };
  renderChips(); listen(); refresh();
  setInterval(refresh, 5000);
})();
