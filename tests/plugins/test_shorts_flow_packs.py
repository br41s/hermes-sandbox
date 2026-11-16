"""Tests for the Flow script packs: parse, validate, and the writer's storage.

Feeds, Drive and the article fetch are faked; nothing leaves the machine.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from plugins.shorts import flow_inbox, flow_packs, series, studio_tool
from plugins.shorts.studio import article

CARPETA = "2026-11-14_es-seo-local"
ARTICLE = "https://biglobster.top/es/ia-para-pymes/seo-local"
STORY = ("La tienda de barrio que no aparece en Google Maps pierde clientes cada día frente a la "
         "que sí sale con buenas reseñas y un mapa que la enseña en la calle.")

CLIP_0_PROMPT = (
    "Wide establishing shot of a small neighbourhood shop at dawn: the shutter is still down, the "
    "street is empty and the window display is dark, and the camera slowly pushes in on the closed "
    "door while the first light of the day hits the awning.\n"
    f"{flow_packs.PROMPT_END}"
)
CLIP_1_PROMPT = (
    "Medium shot inside the shop: Lucía opens the shutter while Martín leans on the counter, and "
    "the camera stays on the two of them as the street wakes up. "
    f"{flow_packs.VOICE_LINE['es']} "
    'Lucía says: "He buscado tu tienda en Google Maps y no aparece por ninguna parte." '
    'Martín nods and answers: "Eso mismo me dice un cliente cada semana, y mientras tanto la '
    'competencia sale primero." '
    "The camera follows a neighbour walking past the window and looking at the empty shelves.\n"
    f"{flow_packs.PROMPT_END}"
)
CLIP_2_PROMPT = (
    "Over-the-shoulder shot of Martín showing a phone to Lucía, both behind the counter, as a "
    f"customer waits at the door. {flow_packs.VOICE_LINE['es']} "
    'Martín says: "Mira, la ficha de tu negocio está vacía y sin fotos, así que nadie se fía de '
    'ella." '
    'Lucía replies: "Entonces vamos a llenarla hoy mismo con las fotos de la tienda, el horario y '
    'dos reseñas de clientes." '
    "The camera cuts to the phone screen filling with the shop's own pictures and a map pin "
    "landing on the street.\n"
    f"{flow_packs.PROMPT_END}"
)
CLIP_3_PROMPT = (
    "Close-up of Lucía's hands opening the local business profile on a laptop, Martín standing "
    f"next to her with the phone. {flow_packs.VOICE_LINE['es']} "
    'Lucía says: "Ya está, ahora quien busque una tienda de barrio en el mapa va a encontrar la '
    'tuya con sus fotos y sus reseñas." '
    'Martín adds: "Y el vecino que pregunta por la calle ya no se va a ir a la tienda de al '
    'lado." '
    "The camera pulls back to the open door and a first customer walking in.\n"
    f"{flow_packs.PROMPT_END}"
)
CLIP_4_PROMPT = (
    "Final wide shot from outside the shop: Lucía and Martín stand at the door with a customer, "
    f"the window display is full of photos and the street is busy. {flow_packs.VOICE_LINE['es']} "
    'Lucía says: "Si tu tienda no sale en el mapa, hoy es el día de arreglarlo: pide ayuda y '
    'empieza por las fotos." '
    'Martín answers: "No hace falta ser un experto, solo rellenar la ficha y pedir a tus clientes '
    'que te dejen una reseña." '
    "The camera tilts up to the shop sign while a small map pin glows on the phone in the "
    "customer's hand.\n"
    f"{flow_packs.PROMPT_END}"
)


def _clip(number: int, name: str, seconds: int, prompt: str) -> str:
    return f"### Clip {number} — {name} ({seconds} s)\n```\n{prompt}\n```\n"


CLIP_0 = _clip(0, "fachada", 8, CLIP_0_PROMPT)
CLIP_1 = _clip(1, "mostrador", 8, CLIP_1_PROMPT)
CLIP_2 = _clip(2, "movil", 8, CLIP_2_PROMPT)
CLIP_3 = _clip(3, "perfil", 8, CLIP_3_PROMPT)
CLIP_4 = _clip(4, "cierre", 8, CLIP_4_PROMPT)
ROTULOS = (
    "## Rótulos\n"
    "| Clip | Posición | Retardo (s) | Texto |\n"
    "|---|---|---|---|\n"
    "| 0 | arriba | 0 | ¿Tu tienda vacía? |\n"
    "| 1 | abajo | 1 | Atrae más clientes hoy |\n"
)
GOOD = (
    "# Tu tienda no aparece en Google Maps\n\n"
    f"Artículo: {ARTICLE}\n"
    "Idioma: es\n\n"
    f"## Historia\n{STORY}\n\n"
    "## Clips\n"
    f"{CLIP_0}\n{CLIP_1}\n{CLIP_2}\n{CLIP_3}\n{CLIP_4}\n"
    f"{ROTULOS}\n"
    "## Portada\n"
    "Título: Tu tienda no aparece en Google\n"
    "Rótulo: SEO local\n\n"
    "## Antes de rodar\n"
    "- Graba en vertical con el móvil y con buena luz.\n"
    "- Deja dos segundos de silencio antes de hablar.\n"
)


def _problems(markdown: str, carpeta: str = CARPETA) -> list[str]:
    return flow_packs.validate(markdown, carpeta)


def _expect(markdown: str, *needles: str, carpeta: str = CARPETA) -> str:
    problems = _problems(markdown, carpeta)
    joined = " | ".join(problems)
    for needle in needles:
        assert any(needle in problem for problem in problems), f"{needle!r} not in {joined!r}"
    return joined


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

def test_parse_reads_the_clips_overlays_and_cover():
    pack = flow_packs.parse(GOOD)
    assert pack["title"] == "Tu tienda no aparece en Google Maps"
    assert pack["article_url"] == ARTICLE
    assert pack["lang"] == "es"
    assert pack["story"] == STORY
    assert [clip["index"] for clip in pack["clips"]] == [0, 1, 2, 3, 4]
    assert pack["clips"][1]["name"] == "mostrador"
    assert pack["clips"][1]["seconds"] == 8.0
    assert flow_packs.VOICE_LINE["es"] in pack["clips"][1]["prompt"]
    assert pack["overlays"] == [
        {"clip": 0, "position": "top", "delay_s": 0.0, "text": "¿Tu tienda vacía?"},
        {"clip": 1, "position": "bottom", "delay_s": 1.0, "text": "Atrae más clientes hoy"},
    ]
    assert pack["cover"] == {"title": "Tu tienda no aparece en Google", "label": "SEO local"}
    for section in ("Historia", "Clips", "Rótulos", "Portada", "Antes de rodar"):
        assert section in pack["sections"]


def test_parse_of_nothing_is_empty_not_an_error():
    pack = flow_packs.parse("")
    assert pack["clips"] == [] and pack["overlays"] == []
    assert pack["cover"] == {"title": None, "label": None}


# ---------------------------------------------------------------------------
# validate: the good pack, then one break per rule
# ---------------------------------------------------------------------------

def test_the_good_pack_validates():
    assert _problems(GOOD) == []


def test_three_clips_is_too_few():
    _expect(GOOD.replace(CLIP_3, "").replace(CLIP_4, ""), "3 clips", "4 to 6")


def test_seven_clips_is_too_many():
    seven = GOOD.replace("(8 s)", "(6 s)")
    extra = f"{_clip(5, 'extra', 6, CLIP_4_PROMPT)}\n{_clip(6, 'cierre', 6, CLIP_4_PROMPT)}"
    seven = seven.replace("\n## Rótulos", f"\n{extra}\n## Rótulos", 1)
    _expect(seven, "7 clips", "4 to 6")


def test_a_clip_of_twelve_seconds_is_refused():
    _expect(GOOD.replace("### Clip 0 — fachada (8 s)", "### Clip 0 — fachada (12 s)"),
            "clips[0]", "3 to 10")


def test_fifty_seconds_in_total_is_refused():
    _expect(GOOD.replace("(8 s)", "(10 s)"), "50 s in total", "45")


def test_a_clip_without_the_fixed_voice_line_is_refused():
    broken = CLIP_2.replace(flow_packs.VOICE_LINE["es"], "")
    _expect(GOOD.replace(CLIP_2, broken), "clips[2].prompt", "voice line")


def test_a_prompt_that_does_not_end_with_the_fixed_line_is_refused():
    _expect(GOOD.replace(flow_packs.PROMPT_END, "", 1), "clips[0].prompt", "must end with")


def test_galician_prose_outside_the_clips_is_refused():
    _expect(GOOD.replace(STORY, "Hoxe a tenda non ten unha web."), "hoxe", "outside")


def test_galician_dialogue_inside_a_quote_is_refused():
    broken = CLIP_1.replace(
        '"He buscado tu tienda en Google Maps y no aparece por ninguna parte."',
        '"Hoxe a tenda non ten unha web e moito menos cando xa é tarde."')
    _expect(GOOD.replace(CLIP_1, broken), "quoted line has foreign words", "hoxe")


def test_english_dialogue_in_an_es_pack_is_refused():
    broken = CLIP_1.replace(
        '"He buscado tu tienda en Google Maps y no aparece por ninguna parte."',
        '"The shop is not on the map and the customers are walking to the other store with your '
        'neighbours tonight."')
    _expect(GOOD.replace(CLIP_1, broken), "clips[1].prompt", "is English")


def test_a_spanish_prompt_body_is_refused():
    spanish = ("La cámara abre en la tienda vacía al amanecer, las estanterías están desnudas y "
               "la calle de fuera está tranquila, sin ninguna señal del dueño.\n"
               f"{flow_packs.PROMPT_END}")
    _expect(GOOD.replace(CLIP_0, _clip(0, "fachada", 8, spanish)), "clips[0].prompt", "English")


def test_an_es_pack_needs_a_slash_es_slash_url():
    broken = GOOD.replace(ARTICLE, "https://biglobster.top/blog/seo-local")
    _expect(broken, "Artículo", "/es/")


def test_a_pack_without_the_rotulos_section_is_refused():
    _expect(GOOD.replace(ROTULOS, ""), "## Rótulos")


def test_a_rotulo_word_that_is_too_long_is_refused():
    _expect(GOOD.replace("Atrae más clientes hoy", "x" * 41), "rótulos[1]", "1 to 40")


def test_idioma_en_with_an_es_carpeta_is_refused():
    problems = _problems(GOOD.replace("Idioma: es", "Idioma: en"))
    assert any("carpeta" in problem and "Idioma: en" in problem for problem in problems)


def test_one_spanish_polo_is_not_flagged():
    assert _problems(GOOD.replace(STORY, STORY + " Comió un polo de limón.")) == []


# ---------------------------------------------------------------------------
# next_pack
# ---------------------------------------------------------------------------

ES_ITEMS = [("Post uno", "https://biglobster.top/es/post-uno"),
            ("Post dos", "https://biglobster.top/es/post-dos"),
            ("Post tres", "https://biglobster.top/es/post-tres"),
            ("Post cuatro", "https://biglobster.top/es/post-cuatro")]
EN_ITEMS = [("Post one", "https://biglobster.top/post-one")]


def _rss(items: list[tuple[str, str]]) -> str:
    rows = "".join(f"<item><title>{title}</title><link>{url}</link>"
                   f"<pubDate>Mon, 16 Nov 2026 08:00:00 +0000</pubDate></item>"
                   for title, url in items)
    return f"<?xml version='1.0'?><rss><channel>{rows}</channel></rss>"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: tmp_path, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def drive(monkeypatch):
    """An in-memory Drive behind the three flow_inbox helpers the writer uses."""
    files: dict[str, dict] = {}
    monkeypatch.setattr(flow_inbox, "_list_dirs", lambda: list(files))
    monkeypatch.setattr(flow_inbox, "_list_files", lambda carpeta: [
        {"Name": name, "Size": 1} for name in files.get(carpeta, {}) if "/" not in name])

    def write_text(carpeta, path, text):
        files.setdefault(carpeta, {})[path] = text

    monkeypatch.setattr(flow_inbox, "_write_text", write_text)
    return files


@pytest.fixture
def feeds(monkeypatch):
    """The two blog feeds and one article, served from memory."""
    monkeypatch.setattr(studio_tool, "_feeds", lambda: {
        "es": "https://biglobster.top/es/feed.xml", "en": "https://biglobster.top/feed.xml"})
    monkeypatch.setattr(studio_tool, "_http_get", lambda url: _rss(
        ES_ITEMS if url.endswith("/es/feed.xml") else EN_ITEMS))
    monkeypatch.setattr(article, "fetch_html", lambda url: "<html><body>hola</body></html>")
    monkeypatch.setattr(article, "html_to_text", lambda html: ("Artículo", "un texto de prueba"))
    return {"es": ES_ITEMS, "en": EN_ITEMS}


def _seed_index(entries: dict[str, dict]) -> None:
    index = flow_packs._load_index()
    index.update(entries)
    flow_packs._save_index(index)


def _entry(lang: str, article_url: str, *, state: str = "written",
           written_at: str = "2020-01-01T00:00:00Z") -> dict:
    return {"lang": lang, "article_url": article_url, "title": "t", "state": state,
            "written_at": written_at}


def test_next_pack_picks_the_language_whose_last_pack_is_oldest(env, drive, feeds):
    _seed_index({"2020-01-01_es-viejo": _entry("es", ARTICLE),
                 "2020-02-02_en-reciente": _entry("en", "https://biglobster.top/post-one")})
    result = flow_packs.next_pack()
    assert result["lang"] == "es"
    assert result["article"] == {"url": "https://biglobster.top/es/post-uno", "title": "Post uno"}
    assert flow_packs.CARPETA.fullmatch(result["carpeta"])
    assert result["carpeta"].endswith("_es-post-uno")
    assert result["voice_line"] == flow_packs.VOICE_LINE["es"]
    assert result["prompt_end"] == flow_packs.PROMPT_END
    assert result["text"] == "un texto de prueba"


def test_next_pack_starts_with_the_first_configured_language(env, drive, feeds):
    assert flow_packs.next_pack()["lang"] == "es"


def test_next_pack_skips_articles_already_in_a_pack_a_short_or_an_episode(env, drive, feeds):
    _seed_index({
        "2020-01-01_es-viejo": _entry("es", "HTTPS://BigLobster.top/es/post-uno?utm_source=x"),
        "2020-02-02_en-reciente": _entry("en", "https://biglobster.top/post-one"),
    })
    flow_inbox._save({"2020-01-01_es-short": {"article_url": "https://biglobster.top/es/post-dos/"}})
    series._save({"msg-1": {"articulo": "https://biglobster.top/es/post-tres"}})
    result = flow_packs.next_pack()
    assert result["lang"] == "es"
    assert result["article"]["url"] == "https://biglobster.top/es/post-cuatro"


def test_next_pack_blocks_while_packs_wait_for_the_owner(env, drive, feeds, monkeypatch):
    monkeypatch.setenv("FLOW_PACKS_WAITING_MAX", "1")
    _seed_index({CARPETA: _entry("es", ARTICLE, state="review")})
    result = flow_packs.next_pack()
    assert "blocked" in result
    assert "1 packs wait for the owner to film them" in result["blocked"]
    assert CARPETA in result["blocked"]


def test_next_pack_returns_the_five_newest_lessons(env, drive, feeds):
    _seed_index({"2020-01-01_es-viejo": _entry("es", ARTICLE),
                 "2020-02-02_en-reciente": _entry("en", "https://biglobster.top/post-one")})
    folder = flow_packs._lessons_dir()
    folder.mkdir(parents=True)
    for day in range(7):
        path = folder / f"2026-11-0{day}_es-leccion-{day}.diff"
        path.write_text(f"--- escrito\n+++ rodado\n+lección {day}\n", encoding="utf-8")
        os.utime(path, (1_700_000_000 + day, 1_700_000_000 + day))
    lessons = flow_packs.next_pack()["lessons"]
    assert [item["carpeta"] for item in lessons] == [
        "2026-11-06_es-leccion-6", "2026-11-05_es-leccion-5", "2026-11-04_es-leccion-4",
        "2026-11-03_es-leccion-3", "2026-11-02_es-leccion-2"]
    assert lessons[0]["diff"].endswith("+lección 6")


# ---------------------------------------------------------------------------
# write / release
# ---------------------------------------------------------------------------

def test_write_holds_the_first_packs_for_review_then_writes_to_drive(env, drive):
    carpetas = ["2026-11-10_es-pack-uno", "2026-11-11_es-pack-dos", "2026-11-12_es-pack-tres"]
    for carpeta in carpetas:
        result = flow_packs.write(carpeta, GOOD)
        assert result["state"] == "review"
        assert flow_packs._load_index()[carpeta]["state"] == "review"
    assert drive == {}                                # nothing reaches the owner yet
    for carpeta in carpetas:
        assert flow_packs.release(carpeta)["state"] == "written"
    result = flow_packs.write("2026-11-13_es-pack-cuatro", GOOD)
    assert result["state"] == "written"
    assert drive["2026-11-13_es-pack-cuatro"]["prompts.md"] == GOOD
    assert "Flow/2026-11-13_es-pack-cuatro/prompts.md" in result["message"]


def test_write_refuses_a_pack_that_does_not_validate(env, drive):
    with pytest.raises(flow_inbox.FlowError) as excinfo:
        flow_packs.write(CARPETA, GOOD.replace("(8 s)", "(12 s)"))
    assert "3 to 10" in str(excinfo.value) and drive == {}


def test_write_refuses_a_carpeta_that_already_has_a_pack(env, drive):
    flow_packs.write(CARPETA, GOOD)
    with pytest.raises(flow_inbox.FlowError) as excinfo:
        flow_packs.write(CARPETA, GOOD)
    assert "already has a pack" in str(excinfo.value)


def test_write_refuses_a_carpeta_that_is_already_a_drive_folder(env, drive):
    drive[CARPETA] = {"prompts.md": GOOD}
    with pytest.raises(flow_inbox.FlowError) as excinfo:
        flow_packs.write(CARPETA, GOOD)
    assert "already in Drive" in str(excinfo.value)


def test_release_uploads_the_stored_pack_after_a_review_edit(env, drive):
    flow_packs.write(CARPETA, GOOD)
    stored = env / "workspace" / "shorts" / "flow" / "packs" / f"{CARPETA}.md"
    edited = GOOD.replace("- Deja dos segundos de silencio antes de hablar.",
                          "- Deja dos segundos de silencio y mira a cámara antes de hablar.")
    stored.write_text(edited, encoding="utf-8")
    assert flow_packs.release(CARPETA)["state"] == "written"
    assert drive[CARPETA]["prompts.md"] == edited
    entry = flow_packs._load_index()[CARPETA]
    assert entry["released_at"] == entry["written_at"]


def test_release_validates_the_stored_pack_again(env, drive):
    flow_packs.write(CARPETA, GOOD)
    stored = env / "workspace" / "shorts" / "flow" / "packs" / f"{CARPETA}.md"
    stored.write_text(GOOD.replace("(8 s)", "(12 s)"), encoding="utf-8")
    with pytest.raises(flow_inbox.FlowError) as excinfo:
        flow_packs.release(CARPETA)
    assert "3 to 10" in str(excinfo.value)
    assert CARPETA not in drive


def test_release_needs_a_pack_in_review(env, drive):
    with pytest.raises(flow_inbox.FlowError):
        flow_packs.release(CARPETA)


# ---------------------------------------------------------------------------
# record_lesson
# ---------------------------------------------------------------------------

def test_record_lesson_keeps_only_a_real_difference(env, drive):
    flow_packs.write(CARPETA, GOOD)
    assert flow_packs.record_lesson(CARPETA, GOOD) is None
    assert flow_packs.record_lesson(CARPETA, GOOD.replace("SEO local", "SEO local  ")) is None
    filmed = GOOD.replace("Atrae más clientes hoy", "Atrae más clientes esta semana")
    path = flow_packs.record_lesson(CARPETA, filmed)
    assert path is not None and path.is_file()
    body = path.read_text(encoding="utf-8")
    assert body.startswith("--- escrito") and "+++ rodado" in body
    assert "+| 1 | abajo | 1 | Atrae más clientes esta semana |" in body


def test_record_lesson_without_a_stored_pack_writes_nothing(env):
    assert flow_packs.record_lesson(CARPETA, GOOD) is None


def test_the_guide_example_passes_the_validator():
    """The writer copies the example in its prompt: if the tool rejects it, every pack starts wrong."""
    root = Path(__file__).resolve().parents[2]
    guide = (root / "shorts" / "biglobster-flow-guionista.prompt").read_text(encoding="utf-8")
    example = guide.split("la forma es esta)\n\n", 1)[1].split("\n══", 1)[0]
    assert flow_packs.validate(example, "2026-11-16_es-seo-local-pymes-espana-2026") == []
