import { test } from "node:test";
import assert from "node:assert/strict";
import { validateAssistantOutput } from "./assistant.js";

function validRaw(overrides = {}) {
  return JSON.stringify({
    reply: "Hola, soy el asistente de IA de Acme. ¿En qué puedo ayudarte?",
    answer_status: "answered",
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

test("trims whitespace on string fields", () => {
  const result = validateAssistantOutput(
    validRaw({ lead: { name: "  Marta  ", email: null, need: "  reforma cocina  " } }),
  );
  assert.equal(result.lead.name, "Marta");
  assert.equal(result.lead.need, "reforma cocina");
});
