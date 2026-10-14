/**
 * Telegram alert on every handoff: the moment a conversation becomes a
 * person's, somebody has to know. One message per escalation, into the
 * shared @b_l_hermes_bot and the forum topic of the customer (registry
 * `telegram: { chatId, threadId }`). Send-only: this service must never
 * poll the token (one poller per bot, and that is Hermes).
 *
 * Nothing here may break a turn: no configuration -> false, any error ->
 * logged and false. The handoff itself (status, label, reply) is already
 * done by the time this runs.
 */

const TELEGRAM_API = "https://api.telegram.org";
const MAX_QUOTE = 300;

const REASON_LABEL = {
  visitor_requested_human: "el visitante pide hablar con una persona",
  lead_qualified: "lead cualificado",
  sensitive_topic: "tema sensible (precio, queja, legal…)",
  turn_cap_reached: "se agotaron los turnos del bot",
  model_cannot_answer: "el bot no sabe responder",
  invalid_model_output: "fallo del modelo",
  no_knowledge_available: "sin acceso al conocimiento del sitio",
  unsupported_media: "el visitante envió un archivo o audio",
  repeated_question: "pregunta repetida sin resolver",
};

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
}

// Pure, exported for tests. Telegram HTML parse mode: only the tags we emit.
export function handoffAlertText({ companyName, channel, reason, visitorMessage, lead, conversationUrl }) {
  const label = REASON_LABEL[reason] || reason || "traspaso";
  const quote = String(visitorMessage || "").trim();
  const lines = [
    `🔔 <b>${escapeHtml(companyName || "Cliente")}</b> · ${escapeHtml(channel || "chat")} · ${escapeHtml(label)}`,
  ];
  if (quote) lines.push(`«${escapeHtml(quote.length > MAX_QUOTE ? quote.slice(0, MAX_QUOTE) + "…" : quote)}»`);
  const who = [lead?.name, lead?.email].filter(Boolean).join(" · ");
  if (who) lines.push(`👤 ${escapeHtml(who)}`);
  if (lead?.need) lines.push(`🎯 ${escapeHtml(lead.need)}`);
  if (conversationUrl) lines.push(`➡️ <a href="${escapeHtml(conversationUrl)}">Responder en Chatwoot</a>`);
  return lines.join("\n");
}

export function conversationUrl(accountId, conversationId) {
  const base = String(process.env.CHATWOOT_BASE_URL || "").replace(/\/+$/, "");
  return base ? `${base}/app/accounts/${accountId}/conversations/${conversationId}` : null;
}

async function sendMessage(token, body) {
  const res = await fetch(`${TELEGRAM_API}/bot${token}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok && data.ok !== false, status: res.status, description: data.description || "" };
}

// Returns true when an alert went out. A thread that no longer exists
// (topic deleted) must not swallow the alert: retry in the group root.
export async function notifyHandoff(entry, details) {
  const token = process.env.TELEGRAM_BOT_TOKEN;
  const chatId = entry?.telegram?.chatId;
  if (!token || !chatId) return false;
  const threadId = entry.telegram.threadId;
  const text = handoffAlertText({ companyName: entry.companyName, ...details });
  const base = { chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true };
  try {
    let r = await sendMessage(token, threadId ? { ...base, message_thread_id: threadId } : base);
    if (!r.ok && threadId && /thread not found/i.test(r.description)) {
      console.error(`telegram topic ${threadId} not found for ${entry.companyName}; posting to the group root`);
      r = await sendMessage(token, base);
    }
    if (!r.ok) {
      console.error(`telegram alert failed for ${entry.companyName}: ${r.status} ${r.description}`);
      return false;
    }
    return true;
  } catch (err) {
    console.error(`telegram alert failed for ${entry.companyName}: ${err.message}`);
    return false;
  }
}
