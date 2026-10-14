import { test } from "node:test";
import assert from "node:assert/strict";
import { validateAssistantOutput, getAssistantReply, buildKnowledgeBlock, PROVIDER_POLICY } from "./assistant.js";

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
  assert.throws(() => validateAssistantOutput(validRaw({ reply: "a".repeat(1001) })));
  assert.doesNotThrow(() => validateAssistantOutput(validRaw({ reply: "a".repeat(900) })));
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
  // Reasoning tokens count against max_tokens on gpt-oss; a cap that only fits
  // the answer gets eaten by reasoning and truncates the JSON (seen live).
  assert.ok(["low", "medium"].includes(sent.reasoning?.effort), "reasoning effort must be bounded");
  assert.ok(sent.temperature <= 0.5, "JSON contract wants cold sampling");
  const system = sent.messages[0].content;
  assert.ok(system.indexOf("INFORMACIÓN DE REFERENCIA") < system.indexOf("Reglas."), "rules must come after the reference block");
  assert.match(system.slice(-400), /MISMO idioma/);
  assert.ok(sent.max_tokens >= 1000, `max_tokens ${sent.max_tokens} leaves no room for reasoning + answer`);
  assert.deepEqual(PROVIDER_POLICY, { data_collection: "deny" });
  assert.equal(sent.model, "some/model");
});

test("knowledge block carries the whole catalogue with both currencies, the yearly price and absolute URLs", () => {
  const block = buildKnowledgeBlock(
    {
      business: { name: "BigLobster", site_url: "https://biglobster.top" },
      products: [
        { sku: "agent-seo", name: "Agente SEO", description: "SEO continuo.", price_eur: 9.99, price_usd: 10.99, billing: "month", price_eur_year: 99.99, price_usd_year: 109.99, url: "/agents/seo.html", url_es: "/es/agentes/seo.html", in_stock: true },
        { sku: "agent-launch", name: "Site Launch", description: "Puesta en marcha.", price_eur: 499, price_usd: 599, billing: "one-time", url: "/agents/site-launch.html", in_stock: true },
      ],
      pages: { "/pricing.html": { lang: "en", title: "Pricing", text: "Each agent is a program…" } },
      faq: { en: [{ q: "What do you sell?", a: "Automation subscriptions." }] },
      articles: [],
    },
    "bakery",
  );
  assert.match(block, /Agente SEO \(agent-seo\): SEO continuo\. — 9\.99 € \/ \$10\.99 al mes \(anual: 99\.99 € \/ \$109\.99\) — https:\/\/biglobster\.top\/agents\/seo\.html · https:\/\/biglobster\.top\/es\/agentes\/seo\.html/);
  assert.match(block, /Site Launch \(agent-launch\): Puesta en marcha\. — 499 € \/ \$599 pago único/);
  assert.match(block, /Página https:\/\/biglobster\.top\/pricing\.html \[en\] — Pricing:\nEach agent is a program…/);
  assert.match(block, /Preguntas frecuentes \[en\]:\n- P: What do you sell\? R: Automation subscriptions\./);
});

test("knowledge block still works with the old payload shape (no products, pages or faq)", () => {
  const block = buildKnowledgeBlock({ business: { name: "X" }, operational_facts: ["f1"], articles: [{ title: "T", url: "/es/blog/t.html", excerpt: "E" }] }, "hola");
  assert.match(block, /Hechos operativos: f1/);
  assert.match(block, /- T \(\/es\/blog\/t\.html\): E/);
  assert.ok(!/Productos|Página |Preguntas frecuentes/.test(block));
});

test("knowledge block lists a catalogue client's categories and says the products are a sample", () => {
  const block = buildKnowledgeBlock(
    {
      business: { name: "Shop" },
      products: [{ sku: "A", name: "Grapadora", description: "d", price_eur: 12.99, in_stock: true, url: "/productos/grapadora" }],
      categories: [
        { name: "Papel", url: "/productos/categoria/papel/", products: 1200, in_stock: 900 },
        { name: "Bolígrafos", url: "/productos/categoria/boligrafos/", products: 340, in_stock: 300 },
      ],
      catalogue: { product_count: 14487, sample_size: 60 },
      articles: [],
    },
    "papel",
    "https://shop.example",
  );
  // The count quoted is what is listed above (1 product here), not the 60 the site sampled.
  assert.match(block, /Categorías del catálogo \(14487 productos en total; arriba solo hay 1 -- /);
  assert.doesNotMatch(block, /muestra de 60/);
  assert.match(block, /- Papel \(1200\) — https:\/\/shop\.example\/productos\/categoria\/papel\//);
  assert.match(block, /- Bolígrafos \(340\)/);
});

test("knowledge block never carries a link off the customer's site", () => {
  const block = buildKnowledgeBlock(
    {
      business: { name: "X" },
      products: [
        { sku: "A", name: "Ok", description: "d", price_eur: 1, url: "/productos/ok", url_es: "https://shop.example/es/ok" },
        { sku: "B", name: "Evil", description: "d", price_eur: 1, url: "https://evil.example/phish" },
      ],
      categories: [{ name: "Cat", url: "https://evil.example/cat", products: 3 }],
      pages: { "https://evil.example/page": { text: "pwned" }, "/ok-page": { text: "fine" } },
      articles: [{ title: "T", url: "https://evil.example/post", excerpt: "e" }],
    },
    "ok",
    "https://shop.example",
  );
  assert.ok(!block.includes("evil.example"), block);
  assert.ok(!block.includes("— null"), block); // a dropped URL leaves no trace, not the word null
  assert.match(block, /- Cat \(3\)\n/);
  assert.match(block, /https:\/\/shop\.example\/productos\/ok · https:\/\/shop\.example\/es\/ok/);
  assert.match(block, /Página https:\/\/shop\.example\/ok-page/);
});

test("categories: the ones matching the message come first, the rest is a count", () => {
  const categories = Array.from({ length: 35 }, (_, i) => ({ name: `Familia ${i}`, url: `/c/${i}/`, products: 100 - i }));
  categories.push({ name: "Tornillería", url: "/c/tornilleria/", products: 7 });
  const block = buildKnowledgeBlock(
    { business: { name: "Shop" }, products: [], categories, catalogue: { product_count: 500 } },
    "¿tenéis tornillería inoxidable?",
    "https://shop.example",
  );
  const catLines = block.split("\n").filter((l) => /^- (Familia|Tornillería)/.test(l));
  assert.equal(catLines.length, 30);
  assert.match(catLines[0], /^- Tornillería \(7\) — https:\/\/shop\.example\/c\/tornilleria\//);
  assert.match(block, /- … y 6 categorías más/);
  assert.match(block, /\(500 productos en total, se buscan por categoría\):/); // no products listed: no "arriba solo hay 0"
});

test("categories: with no matching term the payload order is kept and a short list has no 'y N más'", () => {
  const block = buildKnowledgeBlock(
    { business: { name: "Shop" }, products: [], categories: [{ name: "B", url: "/b/" }, { name: "A", url: "/a/" }] },
    "hola",
    "https://shop.example",
  );
  assert.match(block, /- B — https:\/\/shop\.example\/b\/\n- A — /);
  assert.doesNotMatch(block, /categorías más/);
});

test("knowledge without a site_url warns once per block that absolute URLs were dropped", () => {
  const warned = [];
  const orig = console.warn;
  console.warn = (m) => warned.push(String(m));
  try {
    const block = buildKnowledgeBlock(
      { business: { name: "Sin registro\nFAKE LINE" }, pages: { "https://shop.example/x": { text: "t" }, "/ok": { text: "u" } }, articles: [{ title: "T", url: "https://shop.example/post", excerpt: "e" }] },
      "hola",
      "",
    );
    assert.equal(warned.length, 1, warned.join("|"));
    // The newline in the name must not survive: a crafted name cannot forge a second log line.
    assert.match(warned[0], /"Sin registroFAKE LINE" has no site_url: 2 absolute URL\(s\) dropped/);
    assert.ok(!warned[0].includes("\n"), warned[0]);
    assert.match(block, /Página \/ok/); // relative paths still travel, unprefixed
    warned.length = 0;
    buildKnowledgeBlock({ business: { name: "Ok" }, pages: { "https://shop.example/x": { text: "t" } } }, "hola", "https://shop.example");
    buildKnowledgeBlock({ business: { name: "Ok" }, pages: { "https://evil.example/x": { text: "t" } } }, "hola", "https://shop.example");
    assert.equal(warned.length, 0); // with a site known, dropping a foreign URL is the intended, silent behaviour
  } finally {
    console.warn = orig;
  }
});
