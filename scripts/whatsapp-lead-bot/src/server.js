import express from "express";
import webhookRouter from "./webhook.js";
import { registrySize } from "./registry.js";
import { knowledgeCacheState } from "./knowledge.js";

const app = express();

let lastOpenRouterCallAt = null;
let lastOpenRouterCallOk = null;
// Exported so assistant.js could report call outcomes here in a future
// pass; for now health just reports registry/cache state, which is
// enough to answer "is this instance even configured" during the pilot.
export function recordOpenRouterOutcome(ok) {
  lastOpenRouterCallAt = new Date().toISOString();
  lastOpenRouterCallOk = ok;
}

app.get("/health", (req, res) => {
  res.json({
    ok: true,
    registrySize: registrySize(),
    knowledgeCacheState: knowledgeCacheState(),
    lastOpenRouterCallAt,
    lastOpenRouterCallOk,
  });
});

app.use("/webhook", webhookRouter);

const port = Number(process.env.PORT || 8090);
app.listen(port, () => {
  console.log(`whatsapp-lead-bot listening on :${port}`);
});
