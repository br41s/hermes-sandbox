import { test } from "node:test";
import assert from "node:assert/strict";
import {
  shouldProcess,
  resolvePresentedSecret,
  languageFromBrowser,
  escalationMessage,
  dedupeKey,
  expectedSecretFor,
  handleWebhookEvent,
} from "./webhook.js";
import { loadRegistry } from "./registry.js";
import { clearKnowledgeCache } from "./knowledge.js";

const basePayload = {
  event: "message_created",
  message_type: "incoming",
  content_type: "text",
  conversation: { id: 1, status: "pending" },
};

test("processes a normal incoming text message on a bot-owned (pending) conversation", () => {
  assert.equal(shouldProcess(basePayload).process, true);
});

test("ignores non-message_created events", () => {
  const result = shouldProcess({ ...basePayload, event: "conversation_updated" });
  assert.equal(result.process, false);
  assert.equal(result.reason, "wrong_event");
});

test("ignores outgoing messages (the bot's own sent messages re-triggering the webhook)", () => {
  const result = shouldProcess({ ...basePayload, message_type: "outgoing" });
  assert.equal(result.process, false);
  assert.equal(result.reason, "not_incoming");
});

test("never processes a conversation a human owns (status = open) -- the core safety guard", () => {
  const result = shouldProcess({
    ...basePayload,
    conversation: { id: 1, status: "open" },
  });
  assert.equal(result.process, false);
  assert.equal(result.reason, "not_bot_owned");
});

test("never processes a resolved conversation", () => {
  const result = shouldProcess({
    ...basePayload,
    conversation: { id: 1, status: "resolved" },
  });
  assert.equal(result.process, false);
});

test("flags non-text content as unsupported media instead of running the AI pipeline", () => {
  const result = shouldProcess({ ...basePayload, content_type: "image" });
  assert.equal(result.process, false);
  assert.equal(result.reason, "unsupported_media");
});

test("resolvePresentedSecret accepts a Bearer header", () => {
  assert.equal(resolvePresentedSecret("Bearer abc123", undefined), "abc123");
});

test("resolvePresentedSecret falls back to the query param when no header is present", () => {
  assert.equal(resolvePresentedSecret(undefined, "abc123"), "abc123");
  assert.equal(resolvePresentedSecret("", "abc123"), "abc123");
});

test("resolvePresentedSecret prefers a non-Bearer header over the query param if both are present", () => {
  // Not the expected shape (should be "Bearer <token>"), but if a raw
  // header value is present it's still what gets checked, not silently
  // ignored in favour of the query param.
  assert.equal(resolvePresentedSecret("abc123", "xyz789"), "abc123");
});

test("resolvePresentedSecret returns empty string when neither is present", () => {
  assert.equal(resolvePresentedSecret(undefined, undefined), "");
});

test("a qualified lead gets the model's real reply plus a handoff notice, in its language", () => {
  const modelOutput = { reply: "Yes, we build websites for bakeries." };
  const msg = escalationMessage({ reason: "lead_qualified", modelOutput, lang: "en" });
  assert.ok(msg.startsWith("Yes, we build websites for bakeries."));
  assert.ok(msg.includes("passing you to a person"));
  assert.ok(!msg.includes("don't have enough information"));
});

test("every other escalation reason sends only the fixed safe reply", () => {
  const modelOutput = { reply: "Our price is 10 €" }; // must not go out
  for (const reason of ["sensitive_topic", "model_cannot_answer", "turn_cap_reached", "invalid_model_output"]) {
    const msg = escalationMessage({ reason, modelOutput, lang: "en" });
    assert.ok(msg.startsWith("I don't have enough information"), reason);
    assert.ok(!msg.includes("10 €"), reason);
  }
});

test("a visitor who asked for a person is told so, not that the bot lacks information", () => {
  const msg = escalationMessage({ reason: "visitor_requested_human", modelOutput: null, lang: "en" });
  assert.ok(msg.startsWith("Of course."));
  assert.ok(!msg.includes("don't have enough information"));
  assert.ok(escalationMessage({ reason: "visitor_requested_human", modelOutput: null, lang: "es" }).startsWith("Claro."));
});

test("escalationMessage falls back to Spanish for an unknown language", () => {
  const msg = escalationMessage({ reason: "lead_qualified", modelOutput: { reply: "Oui" }, lang: "fr" });
  assert.ok(msg.includes("Te paso con una persona"));
});

test("only message_created events take part in delivery dedupe (ids of other events are conversation ids)", () => {
  assert.equal(dedupeKey({ ...basePayload, id: 2 }), 2);
  assert.equal(dedupeKey({ event: "conversation_updated", id: 2 }), null);
  assert.equal(dedupeKey({ event: "webwidget_triggered", id: 3 }), null);
  assert.equal(dedupeKey({ ...basePayload }), null); // no id at all
});

test("languageFromBrowser reduces a browser locale to a 2-letter code", () => {
  assert.equal(languageFromBrowser("en-US"), "en");
  assert.equal(languageFromBrowser("es"), "es");
  assert.equal(languageFromBrowser("pt_BR"), "pt");
  assert.equal(languageFromBrowser(undefined), null);
  assert.equal(languageFromBrowser(""), null);
});

test("the webhook secret is bound to the account named in the payload", async () => {
  process.env.WEBHOOK_SHARED_SECRET = "shared-secret";
  process.env.REGISTRY_JSON_BASE64 = Buffer.from(
    JSON.stringify({
      "2": { chatwootAccountId: 2, siteUrl: "https://a.example", enabled: true },
      "3": { chatwootAccountId: 3, siteUrl: "https://b.example", enabled: true, webhookSecret: "secret-for-3" },
    }),
  ).toString("base64");
  const { loadRegistry } = await import("./registry.js");
  loadRegistry();
  assert.equal(expectedSecretFor(2), "shared-secret"); // no own secret: the shared one
  assert.equal(expectedSecretFor(3), "secret-for-3"); // own secret: only that one
  assert.equal(expectedSecretFor(999), "shared-secret"); // unknown account: shared (dropped later as unregistered)
  assert.equal(expectedSecretFor(undefined), "shared-secret");
  delete process.env.REGISTRY_JSON_BASE64;
  delete process.env.WEBHOOK_SHARED_SECRET;
  assert.equal(expectedSecretFor(2), "", "unconfigured never matches");
});

// One whole turn with every outside service stubbed: Chatwoot, the site, the
// model and Telegram. `modelOutput` null makes the model call fail.
async function runTurn({ content, modelOutput, status = "pending", attributes = {}, knowledgeOk = true }) {
  const calls = { models: [], sent: [], statuses: [], labels: 0, telegram: 0, search: 0 };
  let liveStatus = status;
  const realFetch = globalThis.fetch;
  const saved = { ...process.env };
  Object.assign(process.env, {
    CHATWOOT_BASE_URL: "https://chat.example",
    CHATWOOT_AGENT_BOT_TOKEN: "bot-token",
    CHATWOOT_USER_API_TOKEN: "user-token",
    OPENROUTER_API_KEY: "test-key",
    VISITOR_AGENT_MODEL: "some/model",
    TELEGRAM_BOT_TOKEN: "telegram-token",
    REGISTRY_JSON_BASE64: Buffer.from(
      JSON.stringify({ "3": { chatwootAccountId: 3, companyName: "Shop", siteUrl: "https://shop.example", knowledgeToken: "k", enabled: true, telegram: { chatId: "-100" } } }),
    ).toString("base64"),
  });
  loadRegistry();
  clearKnowledgeCache();

  const json = (body) => ({ ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) });
  globalThis.fetch = async (url, opts = {}) => {
    const u = String(url);
    const body = opts.body ? JSON.parse(opts.body) : null;
    if (u.startsWith("https://api.telegram.org")) { calls.telegram += 1; return json({ ok: true }); }
    if (u.startsWith("https://openrouter.ai")) {
      calls.models.push(body);
      if (!modelOutput) return { ok: false, status: 502, text: async () => "bad gateway" };
      return json({ choices: [{ message: { content: JSON.stringify(modelOutput) } }] });
    }
    if (u.endsWith("/api/knowledge")) {
      if (!knowledgeOk) return { ok: false, status: 500 };
      return json({ business: { name: "Shop" }, catalogue: { search_path: "/api/knowledge/search" } });
    }
    if (u.includes("/api/knowledge/search")) { calls.search += 1; return json({ products: [], categories: [] }); }
    if (u.endsWith("/toggle_status")) { liveStatus = body.status; calls.statuses.push(body.status); return json({}); }
    if (u.endsWith("/labels")) { calls.labels += 1; return json({}); }
    if (u.endsWith("/custom_attributes")) return json({});
    if (u.endsWith("/messages") && opts.method === "POST") { calls.sent.push(body.content); return json({}); }
    if (u.endsWith("/messages")) {
      // What Chatwoot returns for a conversation handed off long ago and just
      // reclaimed: the fixed handoff reply, its activity lines, the visitor's
      // new message, then the bot's own "marked as pending" line after it.
      const at = 1_800_000_000;
      return json({ payload: [
        { message_type: 0, content: "¿Hacéis envíos a Canarias?", created_at: at },
        { message_type: 1, content: "No tengo información suficiente para responderte con seguridad. Se lo paso a una persona para que continúe contigo aquí.", created_at: at + 1 },
        { message_type: 2, content: "Bot added needs-attention", created_at: at + 2 },
        { message_type: 2, content: "Conversation was marked open by Bot", created_at: at + 3 },
        { message_type: 0, content, created_at: at + 90_000 },
        { message_type: 2, content: "Conversation was marked as pending by Bot", created_at: at + 90_001 },
      ] });
    }
    return json({ id: 2, status: liveStatus, custom_attributes: attributes });
  };

  try {
    await handleWebhookEvent({
      event: "message_created",
      id: `${Math.random()}`,
      message_type: "incoming",
      content_type: "text",
      content,
      account: { id: 3 },
      inbox: { channel_type: "Channel::WebWidget" },
      conversation: { id: 2, status },
    });
  } finally {
    globalThis.fetch = realFetch;
    for (const key of Object.keys(process.env)) if (!(key in saved)) delete process.env[key];
    Object.assign(process.env, saved);
  }
  return calls;
}

const CANNOT_ANSWER = {
  reply: "No lo sé.",
  answer_status: "cannot_answer",
  detected_language: "es",
  lead: { name: null, email: null, need: null },
  handoff: { required: true, reason: "no information" },
};

test("the 2026-11-04 incident: 'hola' in a reclaimed conversation, model gives up, nobody is alerted", async () => {
  const calls = await runTurn({
    content: "hola",
    modelOutput: CANNOT_ANSWER,
    status: "open",
    attributes: { escalation_at: "2026-10-18T10:00:00Z" },
  });
  assert.deepEqual(calls.statuses, ["pending"], "taken back from the expired handoff and never handed over again");
  assert.equal(calls.labels, 0);
  assert.equal(calls.telegram, 0);
  assert.equal(calls.sent.length, 1);
  assert.ok(!calls.sent[0].includes("No tengo información"), calls.sent[0]);
  assert.ok(!calls.sent[0].includes("No lo sé"), "the give-up text never reaches the visitor");
  assert.match(calls.sent[0], /asistente de inteligencia artificial/);
});

test("a greeting skips the catalogue search: 'no products found for hola' is not a useful fact", async () => {
  const calls = await runTurn({ content: "hola", modelOutput: CANNOT_ANSWER });
  assert.equal(calls.search, 0);
  assert.doesNotMatch(calls.models[0].messages[0].content, /no ha encontrado productos/);
});

test("a greeting the model answers normally goes out as the model wrote it", async () => {
  const calls = await runTurn({
    content: "Hola!",
    modelOutput: { ...CANNOT_ANSWER, reply: "Hola, soy el asistente de IA de Shop.", answer_status: "answered", handoff: { required: false, reason: null } },
  });
  assert.deepEqual(calls.sent, ["Hola, soy el asistente de IA de Shop."]);
  assert.equal(calls.telegram, 0);
});

test("a greeting is still answered when the model fails or the knowledge is down", async () => {
  for (const input of [{ modelOutput: null }, { modelOutput: CANNOT_ANSWER, knowledgeOk: false }]) {
    const calls = await runTurn({ content: "buenos días", ...input });
    assert.equal(calls.telegram, 0);
    assert.deepEqual(calls.statuses, []);
    assert.match(calls.sent[0], /asistente de inteligencia artificial/);
  }
});

test("a greeting at the turn cap gets the fixed reply without paying for a model call", async () => {
  const calls = await runTurn({ content: "thanks!", modelOutput: CANNOT_ANSWER, attributes: { bot_turn_count: 6 } });
  assert.equal(calls.models.length, 0);
  assert.equal(calls.telegram, 0);
  assert.match(calls.sent[0], /AI assistant/);
});

test("a real question the site cannot answer still hands off and alerts", async () => {
  for (const content of ["¿Hacéis envíos a Canarias?", "Hola, ¿hacéis envíos a Canarias?"]) {
    const calls = await runTurn({ content, modelOutput: CANNOT_ANSWER });
    assert.deepEqual(calls.statuses, ["open"], content);
    assert.equal(calls.labels, 1, content);
    assert.equal(calls.telegram, 1, content);
    assert.ok(calls.sent[0].startsWith("No tengo información suficiente"), content);
    assert.equal(calls.search, 1, content);
  }
});
