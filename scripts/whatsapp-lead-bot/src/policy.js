/**
 * Deterministic escalation and lead-qualification policy.
 *
 * The server decides handoff -- never the model. `decideHandoff` combines
 * several independent signals, most of them checkable without any AI
 * call at all (turn cap, missing knowledge, unsupported media, invalid
 * model output). The model's own `handoff.required` is one input among
 * several, honoured but never authoritative on its own: it can trigger an
 * escalation this function wouldn't otherwise have found (e.g. the
 * visitor asked something sensitive in a way our keyword backstop
 * missed), but it can never *prevent* one of the deterministic triggers
 * below from firing.
 *
 * Pure functions, no I/O -- see policy.test.mjs.
 */

// Lightweight, deliberately narrow backstop for topics the brief calls
// out as always-escalate even if the model doesn't flag them itself:
// pricing, availability, commitments, complaints, legal matters. This is
// NOT a substitute for the model's own judgement (a keyword list can't
// understand a paraphrase) -- it exists only to catch the cases where the
// model answers confidently on a topic it should never answer from
// website content alone.
//
// Spanish + English -- at least one live account (BigLobster's own
// biglobster.top dogfood instance) is bilingual, and this backstop must
// not silently go blind just because the visitor happens to write in
// English. Extend with more languages only when a real account needs it.
const SENSITIVE_TOPIC_PATTERNS = [
  /\bprecio\b/i,
  /\bpresupuesto\b/i,
  /\bcu[aá]nto (cuesta|vale)\b/i,
  /\bdisponibilidad\b/i,
  /\bcompromiso\b/i,
  /\bqueja\b/i,
  /\breclamaci[oó]n\b/i,
  /\bdenuncia\b/i,
  /\bdemanda\b/i,
  /\babogad[oa]\b/i,
  /\blegal\b/i,
  /\bprice\b/i,
  /\bpricing\b/i,
  /\bquote\b/i,
  /\bhow much (does|is|would)\b/i,
  /\bavailability\b/i,
  /\bcommitment\b/i,
  /\bcomplaint\b/i,
  /\blawyer\b/i,
  /\battorney\b/i,
];

export function messageTouchesSensitiveTopic(text) {
  if (typeof text !== "string" || !text) return false;
  return SENSITIVE_TOPIC_PATTERNS.some((re) => re.test(text));
}

// Same bilingual-backstop reasoning as SENSITIVE_TOPIC_PATTERNS above.
const HUMAN_REQUEST_PATTERNS = [
  /\b(hablar con (una )?persona|agente humano|operador)\b/i,
  /\b(talk to (a )?(person|human)|human agent|speak (to|with) (a )?(person|representative))\b/i,
];

export function messageRequestsHuman(text) {
  if (typeof text !== "string" || !text) return false;
  return HUMAN_REQUEST_PATTERNS.some((re) => re.test(text));
}

/**
 * @param {object} input
 * @param {string} input.visitorMessage - latest inbound text
 * @param {object|null} input.modelOutput - validated assistant output, or
 *   null if the model call failed / output failed validation
 * @param {number} input.turnCount - bot turns already used in this
 *   conversation, BEFORE this turn
 * @param {number} input.turnCap - configured max bot turns
 * @param {boolean} input.knowledgeAvailable - false if the knowledge
 *   fetch for this account failed and no cached copy existed either
 * @param {boolean} input.hasUnsupportedMedia - true if the inbound
 *   message wasn't plain text
 * @param {boolean} input.visitorRequestedHuman - visitor explicitly asked
 *   for a person (checked by the caller against the raw text; kept as an
 *   input here rather than a regex so callers can use a better heuristic
 *   later without touching this function's contract)
 * @param {boolean} input.repeatedUnresolvedQuestion - visitor repeated a
 *   question after a clarification attempt already failed
 * @returns {{ required: boolean, reason: string|null }}
 */
export function decideHandoff({
  visitorMessage,
  modelOutput,
  turnCount,
  turnCap,
  knowledgeAvailable,
  hasUnsupportedMedia,
  visitorRequestedHuman,
  repeatedUnresolvedQuestion,
}) {
  // Deterministic triggers, checked in a fixed order so the reason
  // recorded on the conversation is always the most specific one, not
  // whichever happened to be checked last.
  if (hasUnsupportedMedia) {
    return { required: true, reason: "unsupported_media" };
  }
  if (visitorRequestedHuman) {
    return { required: true, reason: "visitor_requested_human" };
  }
  if (!knowledgeAvailable) {
    return { required: true, reason: "no_knowledge_available" };
  }
  if (turnCount >= turnCap) {
    return { required: true, reason: "turn_cap_reached" };
  }
  if (!modelOutput) {
    return { required: true, reason: "invalid_model_output" };
  }
  if (modelOutput.answer_status === "cannot_answer") {
    return { required: true, reason: "model_cannot_answer" };
  }
  if (repeatedUnresolvedQuestion) {
    return { required: true, reason: "repeated_question" };
  }
  if (messageTouchesSensitiveTopic(visitorMessage)) {
    return { required: true, reason: "sensitive_topic" };
  }
  if (isLeadQualified(modelOutput.lead)) {
    return { required: true, reason: "lead_qualified" };
  }
  // The model's own signal, honoured last -- everything above is a
  // deterministic override the model can't talk its way out of, but if
  // none of them fired, a model-reported handoff is still respected.
  if (modelOutput.handoff?.required) {
    return {
      required: true,
      reason: modelOutput.handoff.reason || "model_requested_handoff",
    };
  }
  return { required: false, reason: null };
}

// A lead is qualified once we hold a phone (always true once the channel
// is WhatsApp -- the caller supplies it separately, not from `lead`) plus
// a useful description of the need. Name/email are nice-to-haves, never
// required. Deliberately does not look at phone here: phone comes from
// the WhatsApp channel itself, never from the model, so qualification is
// purely about whether the *need* has been captured.
export function isLeadQualified(lead) {
  if (!lead || typeof lead !== "object") return false;
  const need = typeof lead.need === "string" ? lead.need.trim() : "";
  return need.length >= 5;
}
