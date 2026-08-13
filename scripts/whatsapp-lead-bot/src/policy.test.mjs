import { test } from "node:test";
import assert from "node:assert/strict";
import { decideHandoff, isLeadQualified, messageTouchesSensitiveTopic } from "./policy.js";

const baseInput = {
  visitorMessage: "Hola, ¿tenéis servicio de reformas?",
  modelOutput: {
    reply: "Sí, ofrecemos reformas integrales.",
    answer_status: "answered",
    lead: { name: null, email: null, need: null },
    handoff: { required: false, reason: null },
  },
  turnCount: 1,
  turnCap: 6,
  knowledgeAvailable: true,
  hasUnsupportedMedia: false,
  visitorRequestedHuman: false,
  repeatedUnresolvedQuestion: false,
};

test("no escalation when everything is normal and unqualified", () => {
  const result = decideHandoff(baseInput);
  assert.equal(result.required, false);
});

test("escalates on unsupported media regardless of anything else", () => {
  const result = decideHandoff({ ...baseInput, hasUnsupportedMedia: true, modelOutput: null });
  assert.deepEqual(result, { required: true, reason: "unsupported_media" });
});

test("escalates when visitor explicitly asks for a human", () => {
  const result = decideHandoff({ ...baseInput, visitorRequestedHuman: true });
  assert.equal(result.required, true);
  assert.equal(result.reason, "visitor_requested_human");
});

test("escalates when knowledge fetch failed", () => {
  const result = decideHandoff({ ...baseInput, knowledgeAvailable: false });
  assert.equal(result.reason, "no_knowledge_available");
});

test("escalates at the turn cap even with a valid, unqualified model output", () => {
  const result = decideHandoff({ ...baseInput, turnCount: 6, turnCap: 6 });
  assert.equal(result.reason, "turn_cap_reached");
});

test("escalates when model output is null (invalid/failed call)", () => {
  const result = decideHandoff({ ...baseInput, modelOutput: null });
  assert.equal(result.reason, "invalid_model_output");
});

test("escalates when the model reports it cannot answer", () => {
  const result = decideHandoff({
    ...baseInput,
    modelOutput: { ...baseInput.modelOutput, answer_status: "cannot_answer" },
  });
  assert.equal(result.reason, "model_cannot_answer");
});

test("escalates on a sensitive-topic keyword even if the model didn't flag handoff", () => {
  const result = decideHandoff({
    ...baseInput,
    visitorMessage: "¿Cuánto cuesta una reforma integral de 80m2?",
  });
  assert.equal(result.reason, "sensitive_topic");
});

test("escalates once a lead is qualified (need present)", () => {
  const result = decideHandoff({
    ...baseInput,
    modelOutput: {
      ...baseInput.modelOutput,
      lead: { name: "Marta", email: null, need: "Reformar un baño de 4m2" },
    },
  });
  assert.equal(result.reason, "lead_qualified");
});

test("respects the model's own handoff.required as a last-resort signal", () => {
  const result = decideHandoff({
    ...baseInput,
    modelOutput: {
      ...baseInput.modelOutput,
      handoff: { required: true, reason: "visitor sounds frustrated" },
    },
  });
  assert.deepEqual(result, { required: true, reason: "visitor sounds frustrated" });
});

test("deterministic triggers take priority over a model output that claims answered", () => {
  // Model says everything is fine, but the turn cap was already reached --
  // the model cannot talk its way past a deterministic override.
  const result = decideHandoff({ ...baseInput, turnCount: 10, turnCap: 6 });
  assert.equal(result.required, true);
  assert.equal(result.reason, "turn_cap_reached");
});

test("isLeadQualified requires a real need, not just a truthy value", () => {
  assert.equal(isLeadQualified({ need: "" }), false);
  assert.equal(isLeadQualified({ need: "hi" }), false); // too short to be useful
  assert.equal(isLeadQualified({ need: "Necesito presupuesto para tejado" }), true);
  assert.equal(isLeadQualified(null), false);
  assert.equal(isLeadQualified({}), false);
});

test("messageTouchesSensitiveTopic matches Spanish pricing/legal/complaint terms", () => {
  assert.equal(messageTouchesSensitiveTopic("¿Cuál es el precio?"), true);
  assert.equal(messageTouchesSensitiveTopic("Quiero poner una reclamación"), true);
  assert.equal(messageTouchesSensitiveTopic("Hola, buenos días"), false);
});
