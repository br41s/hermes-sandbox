import express from "express";
import webhookRouter from "./webhook.js";
import { registrySize } from "./registry.js";
import { knowledgeCacheState } from "./knowledge.js";
import { openRouterState } from "./health.js";

const app = express();

app.get("/health", (req, res) => {
  res.json({
    ok: true,
    registrySize: registrySize(),
    knowledgeCacheState: knowledgeCacheState(),
    ...openRouterState(),
  });
});

app.use("/webhook", webhookRouter);

const port = Number(process.env.PORT || 8090);
app.listen(port, () => {
  console.log(`whatsapp-lead-bot listening on :${port}`);
});
