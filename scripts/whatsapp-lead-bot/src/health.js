// Last-OpenRouter-call state for /health. Lives in its own module because
// server.js imports webhook.js which imports assistant.js -- assistant.js
// importing the recorder back from server.js would be a cycle. Until
// 2026-10-13 the recorder was declared in server.js and never called, so
// /health reported lastOpenRouterCallAt: null after real calls.
let lastOpenRouterCallAt = null;
let lastOpenRouterCallOk = null;

export function recordOpenRouterOutcome(ok) {
  lastOpenRouterCallAt = new Date().toISOString();
  lastOpenRouterCallOk = Boolean(ok);
}

export function openRouterState() {
  return { lastOpenRouterCallAt, lastOpenRouterCallOk };
}
