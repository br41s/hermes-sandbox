import { test } from "node:test";
import assert from "node:assert/strict";
import {
  shouldProcess,
  resolvePresentedSecret,
  languageFromBrowser,
  escalationMessage,
  dedupeKey,
  expectedSecretFor,
} from "./webhook.js";

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
