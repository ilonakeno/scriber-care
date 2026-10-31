const api = async (path, body) => {
  const res = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json();
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const VERDICT = { bug: "баг", not_bug: "не баг", feature_request: "запит фічі", account_billing: "акаунт / оплата" };
const STATUS = { new: "нове", resolved: "вирішено", waiting_approval: "чекає підтвердження", escalated: "у людини",
  waiting_client: "чекає клієнта", assigned: "у відповідального", executed: "виконано", pending: "чекає", blocked: "заблоковано",
  rejected: "відхилено правилами", declined: "відхилено людиною" };
const EMOTION = { anger: "злість", frustration: "роздратування", anxiety: "тривога", neutral: "нейтрально", satisfaction: "задоволення" };
const CHANNEL = { chat: "чат", review: "відгук", incident: "інцидент", proactive: "проактивно" };

async function renderStatus() {
  const s = await api("/api/status");
  document.getElementById("status").innerHTML = [
    `<span class="pill on">Бізнес: ${esc(s.business)}</span>`,
    `<span class="pill ${s.llm_model ? "on" : "off"}">AI: ${esc(s.llm_model || "запасні правила")}</span>`,
    `<span class="pill ${s.slack ? "on" : "off"}">Slack: ${s.slack ? "підключено" : "лише в застосунку"}</span>`,
    `<span class="pill ${s.github_repo ? "on" : "off"}">GitHub: ${esc(s.github_repo || "чернетки")}</span>`,
  ].join("");
}

function header(active) {
  return `<header class="top"><div class="brand">Customer <span>Care</span></div><nav>
    <a href="/" class="${active === "agent" ? "active" : ""}">Панель агента</a>
    <a href="/chat" class="${active === "chat" ? "active" : ""}">Чат клієнта</a>
    <a href="/dashboard" class="${active === "dashboard" ? "active" : ""}">Дашборд</a>
    <a href="/settings" class="${active === "settings" ? "active" : ""}">Налаштування</a></nav>
    <div class="status" id="status"></div></header>`;
}
