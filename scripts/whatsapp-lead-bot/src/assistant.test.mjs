import { test } from "node:test";
import assert from "node:assert/strict";
import { validateAssistantOutput, getAssistantReply, PROVIDER_POLICY } from "./assistant.js";

function validRaw(overrides = {}) {
  return JSON.stringify({
    reply: "Hola, soy el asistente de IA de Acme. ¿En qué puedo ayudarte?",
    answer_status: "answered",
    detected_language: "es",
    lead: { name: null, email: null, need: null },
    handoff: { required: false, reason: null },
    ...overrides,
  });
}

test("accepts a well-formed model output", () => {
  const result = validateAssistantOutput(validRaw());
  assert.equal(result.answer_status, "answered");
  assert.equal(result.handoff.required, false);
});

test("rejects non-JSON output", () => {
  assert.throws(() => validateAssistantOutput("not json at all"));
});

test("rejects an empty reply", () => {
  assert.throws(() => validateAssistantOutput(validRaw({ reply: "" })));
});

test("rejects a reply that exceeds the max length", () => {
  assert.throws(() => validateAssistantOutput(validRaw({ reply: "a".repeat(1000) })));
});

test("rejects an invalid answer_status enum value", () => {
  assert.throws(() => validateAssistantOutput(validRaw({ answer_status: "maybe" })));
});

test("rejects a missing lead object", () => {
  const raw = JSON.stringify({
    reply: "hola",
    answer_status: "answered",
    handoff: { required: false, reason: null },
  });
  assert.throws(() => validateAssistantOutput(raw));
});

test("rejects a non-boolean handoff.required", () => {
  assert.throws(() =>
    validateAssistantOutput(validRaw({ handoff: { required: "yes", reason: null } })),
  );
});

test("accepts an English-detected reply", () => {
  const result = validateAssistantOutput(
    validRaw({ reply: "Hi, I'm Acme's AI assistant. How can I help?", detected_language: "en" }),
  );
  assert.equal(result.detected_language, "en");
});

test("rejects a missing detected_language", () => {
  const raw = JSON.stringify({
    reply: "hola",
    answer_status: "answered",
    lead: { name: null, email: null, need: null },
    handoff: { required: false, reason: null },
  });
  assert.throws(() => validateAssistantOutput(raw));
});

test("rejects a detected_language that isn't a 2-letter code", () => {
  assert.throws(() => validateAssistantOutput(validRaw({ detected_language: "spanish" })));
  assert.throws(() => validateAssistantOutput(validRaw({ detected_language: "ES" })));
});

test("trims whitespace on string fields", () => {
  const result = validateAssistantOutput(
    validRaw({ lead: { name: "  Marta  ", email: null, need: "  reforma cocina  " } }),
  );
  assert.equal(result.lead.name, "Marta");
  assert.equal(result.lead.need, "reforma cocina");
});

test("every OpenRouter request asks for providers that neither store nor train on the prompt", async () => {
  const realFetch = globalThis.fetch;
  const env = { key: process.env.OPENROUTER_API_KEY, model: process.env.VISITOR_AGENT_MODEL };
  process.env.OPENROUTER_API_KEY = "test-key";
  process.env.VISITOR_AGENT_MODEL = "some/model";
  let sent;
  globalThis.fetch = async (url, opts) => {
    sent = JSON.parse(opts.body);
    return { ok: true, json: async () => ({ choices: [{ message: { content: validRaw() } }] }) };
  };
  try {
    await getAssistantReply({ knowledge: { items: [] }, chatwootMessages: [], visitorMessage: "hola" });
  } finally {
    globalThis.fetch = realFetch;
    if (env.key === undefined) delete process.env.OPENROUTER_API_KEY; else process.env.OPENROUTER_API_KEY = env.key;
    if (env.model === undefined) delete process.env.VISITOR_AGENT_MODEL; else process.env.VISITOR_AGENT_MODEL = env.model;
  }
  assert.deepEqual(sent.provider, { data_collection: "deny" });
  assert.deepEqual(PROVIDER_POLICY, { data_collection: "deny" });
  assert.equal(sent.model, "some/model");
});
