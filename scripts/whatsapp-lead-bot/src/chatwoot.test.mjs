import { test } from "node:test";
import assert from "node:assert/strict";
import { sendMessage, getMessages } from "./chatwoot.js";

const SAMPLE = {
  "2": { chatwootAccountId: 2, siteUrl: "https://a.example", enabled: true },
  "3": { chatwootAccountId: 3, siteUrl: "https://b.example", enabled: true, agentBotToken: "bot-token-for-3" },
};

function stubFetch() {
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, headers: opts.headers });
    return { ok: true, status: 200, json: async () => ({ id: 1 }) };
  };
  return calls;
}

test("an account with its own agent bot sends with that token; the first account keeps the env token", async () => {
  process.env.CHATWOOT_BASE_URL = "https://cw.example";
  process.env.CHATWOOT_AGENT_BOT_TOKEN = "env-bot-token";
  process.env.CHATWOOT_USER_API_TOKEN = "user-token";
  process.env.REGISTRY_JSON_BASE64 = Buffer.from(JSON.stringify(SAMPLE)).toString("base64");
  const { loadRegistry } = await import("./registry.js");
  loadRegistry();
  const calls = stubFetch();
  await sendMessage(3, 10, "hi");
  await sendMessage(2, 11, "hi");
  await getMessages(3, 10);
  assert.equal(calls[0].headers.api_access_token, "bot-token-for-3");
  assert.equal(calls[1].headers.api_access_token, "env-bot-token");
  assert.equal(calls[2].headers.api_access_token, "user-token", "reads keep the user token");
  delete process.env.REGISTRY_JSON_BASE64;
});
