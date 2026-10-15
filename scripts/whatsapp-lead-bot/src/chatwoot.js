/**
 * Chatwoot API client.
 *
 * Every call authenticates with the Agent Bot's `api_access_token` header
 * (not a user token -- this service only ever acts as the bot). Endpoints
 * verified against https://developers.chatwoot.com/api-reference on
 * 2026-08-13:
 *   - POST /conversations/{id}/messages          (send a reply)
 *   - GET  /conversations/{id}                    (re-check status/details)
 *   - GET  /conversations/{id}/messages           (transcript)
 *   - POST /conversations/{id}/toggle_status      (open/pending/resolved/snoozed)
 *   - POST /conversations/{id}/custom_attributes  (lead + bot bookkeeping)
 *   - POST /conversations/{id}/labels             (e.g. "needs-attention")
 *
 * If Chatwoot's actual behaviour differs from this on the live instance,
 * trust the live API over this comment and fix the client -- this was
 * verified against docs, not a running instance (see the implementation
 * brief's own caveat on this point).
 */

import { getRegistryEntry } from "./registry.js";

function baseUrl() {
  const url = process.env.CHATWOOT_BASE_URL;
  if (!url) throw new Error("CHATWOOT_BASE_URL is not configured");
  return url.replace(/\/+$/, "");
}

// Agent bots are scoped to one Chatwoot account, so a second customer needs
// its own bot (platform API) and the registry entry carries that bot's
// token as `agentBotToken`; CHATWOOT_AGENT_BOT_TOKEN stays the fallback for
// the first account and for local runs.
function authToken(accountId) {
  const entry = accountId != null ? getRegistryEntry(accountId) : null;
  const token = entry?.agentBotToken || process.env.CHATWOOT_AGENT_BOT_TOKEN;
  if (!token) throw new Error("CHATWOOT_AGENT_BOT_TOKEN is not configured");
  return token;
}

// Agent-bot tokens can act on a conversation (send, toggle status, labels,
// custom attributes) but Chatwoot refuses them the message-history endpoint
// -- "Access to this endpoint is not authorized for bots", found live
// 2026-10-13. Reads that need the transcript use the per-account
// integration user's token instead. Reads only: the bot never sends as a
// human, so a bot reply stays visibly a bot reply in Chatwoot.
function readToken() {
  const token = process.env.CHATWOOT_USER_API_TOKEN;
  if (!token) throw new Error("CHATWOOT_USER_API_TOKEN is not configured");
  return token;
}

async function chatwootRequest(path, { method = "GET", body, auth = "bot", accountId } = {}) {
  const res = await fetch(`${baseUrl()}${path}`, {
    method,
    headers: {
      "Content-Type": "application/json",
      api_access_token: auth === "user" ? readToken() : authToken(accountId),
    },
    body: body ? JSON.stringify(body) : undefined,
  });

  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(
      `Chatwoot API ${method} ${path} failed: ${res.status} ${text.slice(0, 200)}`,
    );
  }
  if (res.status === 204) return null;
  return res.json();
}

function conversationPath(accountId, conversationId) {
  return `/api/v1/accounts/${accountId}/conversations/${conversationId}`;
}

// Sends exactly one outgoing message. Callers are responsible for never
// calling this more than once per bot turn (constraint: one outbound
// message per bot turn -- every message costs the customer money).
export async function sendMessage(accountId, conversationId, content) {
  return chatwootRequest(`${conversationPath(accountId, conversationId)}/messages`, {
    method: "POST",
    body: { content, message_type: "outgoing" },
    accountId,
  });
}

// Fresh read of the conversation, used right before sending a reply to
// catch the race where a human took over mid-generation -- do not trust
// a status read at the start of the turn, always re-check here.
export async function getConversation(accountId, conversationId) {
  return chatwootRequest(conversationPath(accountId, conversationId), { accountId });
}

// Last N messages for prompt context. Chatwoot returns newest-last or
// newest-first depending on version -- callers must not assume order and
// should sort/slice defensively (see assistant.js).
export async function getMessages(accountId, conversationId) {
  const data = await chatwootRequest(
    `${conversationPath(accountId, conversationId)}/messages`,
    { auth: "user" },
  );
  return Array.isArray(data) ? data : data?.payload || [];
}

// status: "open" | "pending" | "resolved" | "snoozed"
export async function setStatus(accountId, conversationId, status) {
  return chatwootRequest(`${conversationPath(accountId, conversationId)}/toggle_status`, {
    method: "POST",
    body: { status },
    accountId,
  });
}

export async function setCustomAttributes(accountId, conversationId, customAttributes) {
  return chatwootRequest(
    `${conversationPath(accountId, conversationId)}/custom_attributes`,
    { method: "POST", body: { custom_attributes: customAttributes }, accountId },
  );
}

export async function addLabels(accountId, conversationId, labels) {
  return chatwootRequest(`${conversationPath(accountId, conversationId)}/labels`, {
    method: "POST",
    body: { labels },
    accountId,
  });
}
