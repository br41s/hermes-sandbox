/**
 * Chatwoot AgentBot webhook receiver.
 *
 * Chatwoot AgentBot webhooks are NOT HMAC-signed (constraint from the
 * implementation brief) -- the shared secret in WEBHOOK_SHARED_SECRET,
 * checked in constant time, is the only authentication. Reject anything
 * that doesn't present it, before looking at the body at all.
 *
 * The single guard in `shouldProcess` -- text, incoming, pending status,
 * known account -- is what prevents the bot from ever talking over a
 * human. It is checked once here on the webhook snapshot, and checked
 * AGAIN against a fresh Chatwoot read immediately before sending (see
 * `processTurn`), because the webhook's status field can be stale by the
 * time an OpenRouter round trip finishes.
 */

import { Router, json } from "express";
import { timingSafeEqual } from "node:crypto";
import { getRegistryEntry } from "./registry.js";
import { fetchKnowledge } from "./knowledge.js";
import { getAssistantReply } from "./assistant.js";
import { decideHandoff, messageRequestsHuman } from "./policy.js";
import * as chatwoot from "./chatwoot.js";
import { maskPhone, redactBody } from "./redact.js";

const router = Router();

// Fixed, non-model-generated visitor-facing strings. These bypass the LLM
// entirely (that's the point -- a malformed/hallucinating model can't
// corrupt them), so they can't rely on the model's own detected_language
// for anything except the safe-escalation reply, which does have one
// available. Spanish is the fallback when no language signal exists at
// all (e.g. OpenRouter never got called). Extend only when a real account
// needs a third language -- see the note on SENSITIVE_TOPIC_PATTERNS in
// policy.js for the same policy.
const SAFE_ESCALATION_REPLY = {
  es: "No tengo información suficiente para responderte con seguridad. Se lo paso a una persona para que continúe contigo aquí.",
  en: "I don't have enough information to answer that safely. I'm handing this over to a person to continue with you here.",
};

// Appended to the model's own reply when the escalation is a *qualified
// lead*: the visitor got a real answer and is being handed to a person --
// the "not enough information" text would be false there. One message
// still (cost rule): reply + blank line + this.
const LEAD_HANDOFF_NOTICE = {
  es: "Te paso con una persona del equipo para que siga contigo aquí.",
  en: "I'm passing you to a person on the team to continue with you here.",
};

// Pure, exported for testing: the single outbound message for an
// escalation. A qualified lead with a valid model reply gets that reply
// plus the handoff notice; every other reason (cannot answer, invalid
// output, sensitive topic, turn cap, visitor asked for a person...) gets
// the fixed safe reply, because in those cases the model's text is exactly
// what must not go out.
export function escalationMessage({ reason, modelOutput, lang }) {
  const code = SAFE_ESCALATION_REPLY[lang] ? lang : "es";
  if (reason === "lead_qualified" && modelOutput?.reply) {
    return `${modelOutput.reply}\n\n${LEAD_HANDOFF_NOTICE[code]}`;
  }
  return SAFE_ESCALATION_REPLY[code];
}

// No model call happens on this path (see handleUnsupportedMedia below),
// so there is no detected_language to key off of -- sent bilingually in
// one message rather than guessing, which still respects the
// one-outbound-message-per-turn cost rule.
const UNSUPPORTED_MEDIA_REPLY =
  "He recibido tu archivo. Ahora mismo solo puedo leer mensajes de texto, así que voy a pasarle esto a una persona para que lo revise.\n\nI've received your file. Right now I can only read text messages, so I'm passing this along to a person to review.";

// Chatwoot message_created webhook retries -- dedupe on the message id so
// a retry never produces a second reply. Bounded in-memory set: this
// service is stateless-restart-tolerant by design (a lost dedupe entry on
// restart just risks one duplicate reply in the rare restart-mid-retry
// window, not a crash or a wrong recipient), so a DB isn't justified here.
const seenMessageIds = new Set();
const MAX_SEEN = 5000;
function alreadyProcessed(messageId) {
  if (seenMessageIds.has(messageId)) return true;
  seenMessageIds.add(messageId);
  if (seenMessageIds.size > MAX_SEEN) {
    const first = seenMessageIds.values().next().value;
    seenMessageIds.delete(first);
  }
  return false;
}

function secretMatches(presented) {
  const configured = process.env.WEBHOOK_SHARED_SECRET || "";
  if (!configured) return false; // never accept an unconfigured secret
  const a = Buffer.from(presented || "");
  const b = Buffer.from(configured);
  if (a.length !== b.length) return false;
  return timingSafeEqual(a, b);
}

// Chatwoot's Agent Bot "outgoing_url" is configured as a plain URL with no
// documented way to attach a custom Authorization header, so the shared
// secret is accepted either way: as a query param (?secret=..., put
// directly in the outgoing_url Chatwoot is configured with -- this is the
// one guaranteed to work) or as a Bearer header (kept for any deployment
// that can set one). Exported and pure so both paths are testable without
// a full Express request.
export function resolvePresentedSecret(authHeader, querySecret) {
  const header = authHeader || "";
  if (header.startsWith("Bearer ")) return header.slice(7);
  return header || querySecret || "";
}

// "en-US" / "es" / "pt-BR" -> "en" / "es" / "pt"; null when absent. Pure,
// exported for testing. Only the languages SAFE_ESCALATION_REPLY knows
// matter downstream -- anything else falls back to Spanish there.
export function languageFromBrowser(browserLanguage) {
  if (typeof browserLanguage !== "string") return null;
  const code = browserLanguage.trim().toLowerCase().split(/[-_]/)[0];
  return /^[a-z]{2}$/.test(code) ? code : null;
}

// Pure decision, exported for testing: given a webhook payload, should
// this event be handed to the AI pipeline at all?
export function shouldProcess(payload) {
  if (payload?.event !== "message_created") return { process: false, reason: "wrong_event" };
  const messageType = payload?.message_type;
  if (messageType !== "incoming" && messageType !== 0) {
    return { process: false, reason: "not_incoming" };
  }
  if (payload?.content_type && payload.content_type !== "text") {
    return { process: false, reason: "unsupported_media" };
  }
  if (payload?.conversation?.status !== "pending") {
    return { process: false, reason: "not_bot_owned" };
  }
  return { process: true, reason: null };
}

// Route-scoped body parser so this router is mountable standalone without
// relying on server.js to have applied one globally first.
router.post("/", json(), async (req, res) => {
  const presented = resolvePresentedSecret(req.headers.authorization, req.query.secret);
  if (!secretMatches(presented)) {
    // Logged: the first live delivery vanished without a trace because every
    // early-exit here was silent. Keys only -- never the secret or the body.
    console.warn(
      `webhook rejected: bad or missing secret (auth header: ${req.headers.authorization ? "present" : "absent"}, query secret: ${req.query.secret ? "present" : "absent"})`,
    );
    return res.status(401).json({ error: "unauthorized" });
  }

  const payload = req.body;

  // Acknowledge immediately -- never make Chatwoot's webhook delivery
  // wait on an OpenRouter round trip. Everything after this point is
  // fire-and-forget from Chatwoot's perspective.
  res.status(200).json({ received: true });

  try {
    await handleWebhookEvent(payload);
  } catch (err) {
    console.error(`webhook processing failed: ${err.message}`);
  }
});

// Pure, exported for testing. `payload.id` is only a message id on
// message_created; on conversation_updated / webwidget_triggered it is the
// conversation's. Deduping every event by bare id let a conversation's own
// updates poison the seen-set, so a visitor message whose id happened to
// equal a conversation id was dropped as a "duplicate" and never answered
// (the likeliest cause of the unanswered first live widget turn on
// 2026-10-13: the logs of that build did not record drops yet).
export function dedupeKey(payload) {
  if (payload?.event !== "message_created" || payload?.id == null) return null;
  return payload.id;
}

async function handleWebhookEvent(payload) {
  const messageId = dedupeKey(payload);
  if (messageId != null && alreadyProcessed(messageId)) {
    console.log(`webhook ignored: duplicate delivery of message ${messageId}`);
    return;
  }

  const { process: shouldRun, reason } = shouldProcess(payload);
  if (!shouldRun) {
    // One line per drop with the fields the decision was made on -- enough to
    // see a payload-shape mismatch from the logs alone, without any content.
    console.log(
      `webhook ignored: ${reason} (event=${payload?.event} message_type=${payload?.message_type} content_type=${payload?.content_type} conversation=${payload?.conversation?.id} status=${payload?.conversation?.status} id=${payload?.id})`,
    );
    if (reason === "unsupported_media") {
      await handleUnsupportedMedia(payload).catch((err) =>
        console.error(`failed to handle unsupported media: ${err.message}`),
      );
    }
    return;
  }

  await processTurn(payload);
}

async function handleUnsupportedMedia(payload) {
  const accountId = payload?.account?.id;
  const conversationId = payload?.conversation?.id;
  if (!accountId || !conversationId) return;
  await chatwoot.sendMessage(accountId, conversationId, UNSUPPORTED_MEDIA_REPLY);
  await chatwoot.setStatus(accountId, conversationId, "open");
  await chatwoot.addLabels(accountId, conversationId, ["needs-attention"]);
  await chatwoot
    .setCustomAttributes(accountId, conversationId, { escalation_reason: "unsupported_media" })
    .catch(() => {});
}

async function processTurn(payload) {
  const accountId = payload.account.id;
  const conversationId = payload.conversation.id;
  const visitorMessage = String(payload.content || "");

  const entry = getRegistryEntry(accountId);
  if (!entry) {
    console.error(`webhook for unknown/disabled account ${accountId}`);
    return;
  }

  const knowledge = await fetchKnowledge(entry);

  let existingAttributes = {};
  let conversationSnapshot = null;
  try {
    conversationSnapshot = await chatwoot.getConversation(accountId, conversationId);
    existingAttributes = conversationSnapshot?.custom_attributes || {};
  } catch (err) {
    console.error(`could not read conversation ${conversationId}: ${err.message}`);
  }
  const turnCount = Number(existingAttributes.bot_turn_count || 0);
  const turnCap = Number(process.env.BOT_TURN_CAP || 6);

  let modelOutput = null;
  let modelError = null;
  if (knowledge) {
    try {
      const chatwootMessages = await chatwoot.getMessages(accountId, conversationId);
      modelOutput = await getAssistantReply({ knowledge, chatwootMessages, visitorMessage });
    } catch (err) {
      modelError = err;
      console.error(
        `assistant call failed for conversation ${conversationId}: ${err.message}`,
      );
    }
  }

  const decision = decideHandoff({
    visitorMessage,
    modelOutput,
    turnCount,
    turnCap,
    knowledgeAvailable: Boolean(knowledge),
    hasUnsupportedMedia: false, // already filtered out in shouldProcess
    visitorRequestedHuman: messageRequestsHuman(visitorMessage),
    repeatedUnresolvedQuestion: false, // requires history diffing -- left conservative for MVP, see report
  });

  // Race check: re-read status right before sending. If a human took the
  // conversation while we were generating, drop the reply -- this is the
  // exact mechanic that replaces the original design's custom
  // state_version column.
  let liveConversation;
  try {
    liveConversation = await chatwoot.getConversation(accountId, conversationId);
  } catch (err) {
    console.error(`pre-send status re-check failed: ${err.message}`);
    return; // safer to drop than to send blind
  }
  if (liveConversation?.status !== "pending") {
    console.log(
      `dropping reply for conversation ${conversationId}: status changed to ${liveConversation?.status} during generation`,
    );
    return;
  }

  if (decision.required) {
    // modelOutput can be null (e.g. OpenRouter was unavailable) -- fall
    // back to Spanish, matching this service's original default, rather
    // than guessing the visitor's language from scratch here.
    // No model output (e.g. OpenRouter down) means no detected language; the
    // website channel still tells us the visitor's browser language, which
    // beats defaulting an English visitor to Spanish (seen live 2026-10-13).
    const lang =
      modelOutput?.detected_language ||
      languageFromBrowser(conversationSnapshot?.additional_attributes?.browser_language);
    const outbound = escalationMessage({ reason: decision.reason, modelOutput, lang });
    await chatwoot.sendMessage(accountId, conversationId, outbound);
    await chatwoot.setStatus(accountId, conversationId, "open");
    await chatwoot.addLabels(accountId, conversationId, ["needs-attention"]);
    await chatwoot.setCustomAttributes(accountId, conversationId, {
      bot_turn_count: turnCount + 1,
      escalation_reason: decision.reason,
      // Bounded, operator-facing detail on *why* the model output was
      // unusable -- useful when debugging a spike in escalations without
      // needing to grep service logs for the conversation id.
      escalation_detail: modelError ? String(modelError.message).slice(0, 200) : null,
      lead_need: modelOutput?.lead?.need || existingAttributes.lead_need || null,
      lead_name: modelOutput?.lead?.name || existingAttributes.lead_name || null,
      lead_email: modelOutput?.lead?.email || existingAttributes.lead_email || null,
    });
    console.log(
      `escalated conversation ${conversationId} (account ${accountId}, ${maskPhone(payload?.sender?.phone_number)}): ${decision.reason}`,
    );
    return;
  }

  await chatwoot.sendMessage(accountId, conversationId, modelOutput.reply);
  await chatwoot.setCustomAttributes(accountId, conversationId, {
    bot_turn_count: turnCount + 1,
    lead_need: modelOutput.lead.need || existingAttributes.lead_need || null,
    lead_name: modelOutput.lead.name || existingAttributes.lead_name || null,
    lead_email: modelOutput.lead.email || existingAttributes.lead_email || null,
  });
  console.log(
    `replied in conversation ${conversationId}: ${redactBody(modelOutput.reply)}`,
  );
}

export default router;
