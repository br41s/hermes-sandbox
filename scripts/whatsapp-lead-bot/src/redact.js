/**
 * Log-safety helpers.
 *
 * This service handles WhatsApp phone numbers, message bodies, and
 * Chatwoot/OpenRouter tokens -- none of that may reach process logs in
 * full. Every console.log/console.error call in this codebase must route
 * through these helpers rather than interpolating raw values.
 */

// "+34600111222" -> "+34 ••• ••• 222". Keeps the country code and the
// last 3 digits (enough for an operator to eyeball-match a support ticket
// against a customer complaint) and masks everything else.
export function maskPhone(phone) {
  if (typeof phone !== "string" || !phone) return "(sin número)";
  const digits = phone.replace(/[^\d+]/g, "");
  const cc = digits.startsWith("+") ? digits.slice(0, 3) : digits.slice(0, 2);
  const last3 = digits.slice(-3);
  return `${cc} ••• ••• ${last3}`;
}

// Truncates and marks message/reply bodies for logging -- never log the
// full content, only enough to identify *that* something happened.
export function redactBody(text, maxChars = 40) {
  if (typeof text !== "string") return "(vacío)";
  const trimmed = text.trim();
  if (!trimmed) return "(vacío)";
  return trimmed.length > maxChars
    ? `${trimmed.slice(0, maxChars)}… [${trimmed.length} chars]`
    : `[${trimmed.length} chars]`;
}

// Shallow-redacts an object before logging: known-sensitive keys are
// replaced with a fixed marker, everything else passes through as-is.
// Deliberately a denylist, not an allowlist -- new sensitive fields must
// be added here explicitly, but this keeps normal debugging fields
// (conversation ids, account ids, status strings) visible without having
// to enumerate every safe field by hand.
const SENSITIVE_KEYS = new Set([
  "token",
  "access_token",
  "api_access_token",
  "knowledgeToken",
  "authorization",
  "content",
  "phone",
  "phone_number",
  "email",
]);

export function redactForLog(obj) {
  if (obj === null || typeof obj !== "object") return obj;
  const out = Array.isArray(obj) ? [] : {};
  for (const [key, value] of Object.entries(obj)) {
    if (SENSITIVE_KEYS.has(key)) {
      out[key] = "[redacted]";
    } else if (value && typeof value === "object") {
      out[key] = redactForLog(value);
    } else {
      out[key] = value;
    }
  }
  return out;
}
