// Regenerate infographic/inter-metrics.json.
//
// Paste into the devtools console of any page that loads the site webfont
// (https://biglobster.top/ or a client bl-site-package site), then save the
// printed JSON into the "advances_em" field of inter-metrics.json.
//
// WHY MEASURE AND CHECK IN, instead of computing at validation time: the
// validator is a stdlib-only Python script that runs inside a cron sandbox with
// no browser and no font library. The alternative was the 0.55 em/char guess
// that the old prompt asked the model to do in its head, which is what let
// "COMPRAR E INSTALAR" ship clipped to "OMPRAR E INSTALA".
//
// Only weights 400 and 600 are measured because those are the only two the
// infographics use. Neither site loads Inter 700: Chromium falls back to the
// nearest loaded weight (600) with no synthetic widening, so 700 and 600
// measure identically.

(async () => {
  await document.fonts.ready;
  const ctx = document.createElement("canvas").getContext("2d");
  const CHARS =
    " !\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`" +
    "abcdefghijklmnopqrstuvwxyz{|}~" +
    "¡¿ÁÉÍÓÚÜÑáéíóúüñ€·–—‘’“”…«»";

  const out = {};
  for (const weight of [400, 600]) {
    ctx.font = `${weight} 100px Inter, sans-serif`;
    const table = {};
    for (const ch of CHARS) {
      table[ch] = +(ctx.measureText(ch).width / 100).toFixed(4);
    }
    out[weight] = table;
  }

  // Sanity: Inter must actually be loaded, or you are measuring the fallback
  // stack (system-ui), which is wider and would make the guard too permissive.
  const loaded = [...document.fonts]
    .filter((f) => f.family === "Inter" && f.status === "loaded")
    .map((f) => f.weight);
  console.log("Inter weights loaded:", loaded.join(", ") || "NONE — do not use this run");
  console.log(JSON.stringify(out));
})();
