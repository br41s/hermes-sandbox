# Prompt-patch — Slice 1: conciencia a nivel de sitio (onsite-SEO biglobster)

Parche para el prompt vivo en `jobs.json`. Tres cambios: (A) nueva sección
site-state, (B) ruta del ledger por-perfil, (C) tamaño de batch unificado.
Aplicar tras una migración única del ledger (ver nota al final).

Variable de perfil usada abajo: `<profile>` = `biglobster`.
Ruta base de datos de perfil (FUERA del clon): `/opt/data/profiles/<profile>/seo/`.

---

## (A) NUEVA SECCIÓN — insertar tras `[LEDGER DE COBERTURA POR URL]`

**[ESTADO DEL SITIO — GRAFO Y MAPA SEMÁNTICO]**

Capa de conciencia a nivel de SITIO que complementa el ledger por-URL. El ledger
responde "¿qué hice en esta URL?"; el site-state responde "¿cómo se relacionan las
URLs entre sí?". Se reconstruye al inicio de cada ciclo (tras PASO 0) y es de SOLO
LECTURA para las decisiones del día: lo observas, no lo "optimizas".

Ruta canónica (por perfil, FUERA del clon):
`/opt/data/profiles/biglobster/seo/site-state.json`.
NUNCA lo guardes dentro de `/opt/data/biglobster` (el clon compartido con el Gap
Hunter) ni en `.credentials/`.

Esquema:
```json
{
  "updated": "ISO timestamp",
  "built_against_commit": "SHA de main contra el que se construyó el grafo",
  "internal_link_graph": {
    "https://biglobster.top/es/blog/<slug>": {
      "outbound": ["https://biglobster.top/...", "..."],
      "inbound":  ["https://biglobster.top/..."]
    }
  },
  "keyword_url_map": { "<consulta GSC>": ["url1", "url2"] },
  "orphans": ["urls sin inbound"],
  "cannibalization": [ {"query": "...", "urls": ["url1", "url2"]} ]
}
```

Procedimiento de reconstrucción (inicio de ciclo — acotado y de solo lectura):

IMPORTANTE: NO uses `execute_code` ni `python3 -c/-e` (el sandbox de cron los
bloquea). Usa el constructor determinista YA INSTALADO, invocándolo por RUTA con
el tool `terminal`:

1. Ejecuta:
       python3 /opt/data/profiles/biglobster/seo/build_sitestate.py \
         --web-dir /opt/data/biglobster/web \
         --site-base https://biglobster.top \
         --out /opt/data/profiles/biglobster/seo/site-state.json \
         --repo /opt/data/biglobster \
         --pillar https://biglobster.top/ \
         --pillar https://biglobster.top/services.html \
         --pillar https://biglobster.top/pricing.html \
         --pillar https://biglobster.top/contact.html \
         --pillar https://biglobster.top/blog.html \
         --pillar https://biglobster.top/es/blog.html \
         --pillar https://biglobster.top/agents.html \
         --pillar https://biglobster.top/clientes.html \
         --pillar https://biglobster.top/404.html \
         --pillar https://biglobster.top/blog/infografias/canonical-infographic-template.html \
         --merge
   (los dos últimos --pillar excluyen falsos huérfanos: 404 + plantilla infografía)
2. Lee el site-state.json (read_file / cat) para las decisiones del día.
3. (Opcional) Aumenta keyword_url_map con UNA consulta GSC y recalcula
   cannibalization; reescribe con el tool `write_file` (NUNCA python -c).
4. Si el constructor falla (permisos / ausente / web-dir): ALERTA + continúa con
   el ledger por-URL. Un site-state ausente NO aborta el ciclo.

> POR QUÉ HELPER Y NO INLINE: el trace b1d30a47 (2026-06-28) probó que el sandbox
> de cron BLOQUEA `execute_code` y `python3 -c` ("código sin aprobación humana").
> El agente no pudo escribir site-state.json y degradó a grep. El script commiteado
> invocado por ruta es una ejecución de archivo normal → pasa el gate, es
> determinista y auditable.

Uso (NO sustituye a ninguna acción; les da señal persistente y cross-run):
- El trigger de huérfanos ([DETECCIÓN DE DELTAS] #3) y `Flag_canibalización` del
  scoring del PRE-FLIGHT se leen de AQUÍ, de forma determinista, en vez de
  re-derivarse en vivo en cada ciclo.
- Acción 1 (enlazado) prioriza conectar `orphans` hacia contenido pilar.
- Acción 4 (canibalización) parte de `cannibalization` ya calculado.
- Es la fuente que leerán futuros handoffs inter-agente (p.ej. proponer la fusión de
  2 URLs que canibalizan).

---

## (B) RUTA DEL LEDGER POR-PERFIL — reemplazos en `[LEDGER DE COBERTURA POR URL]`
y en `[EXECUTION & SAFETY PROTOCOLS]`

Buscar y reemplazar TODAS las ocurrencias (2):
- `/opt/data/seo/seo-ledger.json`
  → `/opt/data/profiles/biglobster/seo/seo-ledger.json`

Motivo: la ruta global colisiona entre perfiles cuando otro proyecto (finview,
grow-shop) active este mismo agente. La ruta por-perfil lo evita y sigue estando
fuera del clon (segura ante cover-wipe).

---

## (C) TAMAÑO DE BATCH UNIFICADO — en `[PRE-FLIGHT: PRIORIZACIÓN DE URLS]`

Añadir al inicio de la sección un parámetro único:
> `BATCH_SIZE = 1`   # nº máx. de URLs por ciclo (arranque seguro; subir cuando estable)

Y reemplazar las dos frases contradictorias por una sola que use `BATCH_SIZE`:
- ~~"seleccionar el batch óptimo de hasta **1** URLs del día"~~
- ~~"Selecciona las **5** URLs con mayor score"~~
- → "Selecciona las `BATCH_SIZE` URLs con mayor score; documenta el ranking
   completo en el log interno antes de continuar."

---

## (D) WIRING — pequeños punteros para que el site-state se consuma (no quede huérfano)

Tres añadidos de una línea cada uno:
- **Scoring (PRE-FLIGHT), def. de `Flag_canibalización`:** añadir
  "(lee de `cannibalization` en [ESTADO DEL SITIO])".
- **Acción 1 (enlazado):** en "huérfanos recientes", añadir
  "(parte de `orphans` en [ESTADO DEL SITIO])".
- **Acción 4 (canibalización):** sustituir el arranque por
  "Parte de la lista `cannibalization` ya calculada en [ESTADO DEL SITIO]; confirma
  en GSC consultas donde ≥2 URLs…".

---

## DESPLIEGUE — pasos manuales en el volumen (ANTES de la próxima corrida)

El trace b1d30a47 mostró además `Permission denied` en el dir seo (root-owned) y
que el script aún no está instalado. Pasos (ajusta el usuario del agente):

```
# 1. Instala el constructor en la ruta que invoca el prompt
mkdir -p /opt/data/profiles/biglobster/seo
cp build_sitestate.py /opt/data/profiles/biglobster/seo/build_sitestate.py

# 2. Da propiedad del dir al usuario del agente (NO dejar root-owned)
chown -R <usuario-agente>:<usuario-agente> /opt/data/profiles/biglobster/seo
chmod 0755 /opt/data/profiles/biglobster/seo/build_sitestate.py

# 3. Verifica que el agente puede leer/escribir ahí (como ese usuario):
#    python3 /opt/data/profiles/biglobster/seo/build_sitestate.py --help
```

Fuente de record del script: `onsite-seo/build_sitestate.py` en el repo hermes.
(Agnóstico: para otros perfiles, instalar el MISMO script e invocar con sus args.
Seguimiento: instalar vía cont-init en una ruta compartida en vez de copiar a mano.)

Verificación tras la corrida: `python3 -c` NO debe aparecer bloqueado en el trace;
`site-state.json` DEBE existir con `pages>0`; el modo NO debe ser CATCH-UP por
"ledger ilegible".

---

## NOTA OPERATIVA — migración única del ledger (ANTES de activar el parche)

Mover la ruta del ledger SIN migrar el archivo hace que el agente lea
`baseline_complete = false` para todo y dispare un barrido CATCH-UP completo.
Operación única antes del primer run con el prompt parcheado:

```
cp /opt/data/seo/seo-ledger.json \
   /opt/data/profiles/biglobster/seo/seo-ledger.json
```

(crear el directorio destino si no existe). Verificar que el JSON copiado es
válido antes de lanzar el ciclo.
