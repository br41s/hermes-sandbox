import { test } from "node:test";
import assert from "node:assert/strict";
import { handoffAlertText, notifyHandoff, conversationUrl } from "./notify.js";

const entry = { companyName: "BigLobster", telegram: { chatId: "-1001234567890", threadId: 42 } };
const details = {
  channel: "web chat",
  reason: "visitor_requested_human",
  visitorMessage: "can I talk to a <person>?",
  lead: { name: "Marta", email: null, need: "web para panadería" },
  conversationUrl: "https://cw.example/app/accounts/2/conversations/7",
};

function stubFetch(handler) {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    const body = JSON.parse(opts.body);
    calls.push({ url, body });
    const r = handler(body, calls.length);
    return { ok: r.ok, status: r.status, json: async () => r.json };
  };
  return calls;
}

test("alert text: company, channel, reason in words, quoted message, lead, link; HTML escaped", () => {
  const t = handoffAlertText({ companyName: "BigLobster", ...details });
  assert.match(t, /^🔔 <b>BigLobster<\/b> · web chat · el visitante pide hablar con una persona/);
  assert.match(t, /«can I talk to a &lt;person&gt;\?»/);
  assert.match(t, /👤 Marta\n🎯 web para panadería/);
  assert.match(t, /<a href="https:\/\/cw\.example\/app\/accounts\/2\/conversations\/7">Responder en Chatwoot<\/a>/);
});

test("alert text: unknown reason and no lead degrade gracefully", () => {
  const t = handoffAlertText({ companyName: "X", reason: "something_new", visitorMessage: "", lead: null, conversationUrl: null });
  assert.equal(t, "🔔 <b>X</b> · chat · something_new");
});

test("conversationUrl needs CHATWOOT_BASE_URL", () => {
  const prev = process.env.CHATWOOT_BASE_URL;
  process.env.CHATWOOT_BASE_URL = "https://cw.example/";
  assert.equal(conversationUrl(2, 7), "https://cw.example/app/accounts/2/conversations/7");
  delete process.env.CHATWOOT_BASE_URL;
  assert.equal(conversationUrl(2, 7), null);
  if (prev) process.env.CHATWOOT_BASE_URL = prev;
});

test("sends into the customer's topic with the shared token; never throws", async () => {
  process.env.TELEGRAM_BOT_TOKEN = "123:abc";
  const calls = stubFetch(() => ({ ok: true, status: 200, json: { ok: true } }));
  assert.equal(await notifyHandoff(entry, details), true);
  assert.equal(calls.length, 1);
  assert.match(calls[0].url, /\/bot123:abc\/sendMessage$/);
  assert.equal(calls[0].body.chat_id, "-1001234567890");
  assert.equal(calls[0].body.message_thread_id, 42);
  assert.equal(calls[0].body.parse_mode, "HTML");
});

test("a deleted topic falls back to the group root instead of losing the alert", async () => {
  process.env.TELEGRAM_BOT_TOKEN = "123:abc";
  const calls = stubFetch((body, n) =>
    n === 1 ? { ok: false, status: 400, json: { ok: false, description: "Bad Request: message thread not found" } } : { ok: true, status: 200, json: { ok: true } },
  );
  assert.equal(await notifyHandoff(entry, details), true);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].body.message_thread_id, undefined);
});

test("unconfigured (no token or no chat) is a quiet no-op; network failure returns false", async () => {
  delete process.env.TELEGRAM_BOT_TOKEN;
  const calls = stubFetch(() => ({ ok: true, status: 200, json: { ok: true } }));
  assert.equal(await notifyHandoff(entry, details), false);
  process.env.TELEGRAM_BOT_TOKEN = "123:abc";
  assert.equal(await notifyHandoff({ companyName: "NoTelegram" }, details), false);
  assert.equal(calls.length, 0);
  globalThis.fetch = async () => { throw new Error("ECONNRESET"); };
  assert.equal(await notifyHandoff(entry, details), false);
});
