"""Tests for the infographic validator.

The check that has to be right is text-overflow-box. Every other finding is a
pattern match on markup; that one is arithmetic against measured font metrics,
and it is the only check that catches what a reader actually sees. The
regression case is real: a pill on
automatizacion-industrial-pyme-galicia-ayudas-2026 shipped reading
"OMPRAR E INSTALA" because white text overhung a 128-unit pill onto the white
page. It never crossed the viewBox, so a canvas-only check called it clean.

Second thing worth getting right: the validator must not INVENT overflow. The
first version measured every label at the default 16px because 20 legacy
infographics set font-size in a <style> block, and it reported overflows on
articles that render correctly. Measuring the wrong size is worse than not
measuring, because a false alarm trains everyone to skip the check.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "infographic" / "validate_infographic.py"
_spec = importlib.util.spec_from_file_location("validate_infographic", _MODULE_PATH)
mod = importlib.util.module_from_spec(_spec)
sys.modules["validate_infographic"] = mod
_spec.loader.exec_module(mod)


def codes(svg, stack="biglobster"):
    return [f.code for f in mod.validate(svg, stack, skip_spelling=True)]


def svg(body, width=800, height=400):
    return f'<svg viewBox="0 0 {width} {height}">{body}</svg>'


# --------------------------------------------------------------- the canvas

def test_the_standard_canvas_is_accepted():
    assert codes(svg('<text x="20" y="50" font-size="16">Hola</text>')) == []


def test_a_non_standard_canvas_width_is_rejected():
    assert "canvas-width" in codes(svg('<text x="10" y="50" font-size="16">Hola</text>', width=360))


def test_a_missing_viewbox_is_rejected():
    assert "canvas-missing" in codes('<svg><text x="1" y="1" font-size="16">Hola</text></svg>')


# ------------------------------------------------------- text inside its box

def test_the_shipped_pill_regression_is_caught():
    """The real "OMPRAR E INSTALA" case, coordinates as published."""
    found = codes(svg(
        '<rect x="40" y="78" width="340" height="272" rx="12"/>'
        '<rect x="40" y="78" width="128" height="26" rx="13"/>'
        '<text x="104" y="95" font-size="13" font-weight="700" '
        'text-anchor="middle">COMPRAR E INSTALAR</text>'
    ))
    assert "text-overflow-box" in found


def test_a_label_that_fits_its_box_is_not_flagged():
    found = codes(svg(
        '<rect x="40" y="78" width="340" height="60" rx="12"/>'
        '<text x="210" y="112" font-size="16" text-anchor="middle">Corto</text>'
    ))
    assert "text-overflow-box" not in found


def test_a_thin_divider_rect_is_not_treated_as_a_container():
    """A 2-unit rule under a heading must not become the heading's box."""
    found = codes(svg(
        '<rect x="40" y="96" width="60" height="2"/>'
        '<text x="40" y="96" font-size="16">Un titulo bastante largo de verdad</text>'
    ))
    assert "text-overflow-box" not in found


def test_the_smallest_containing_rect_wins():
    """Text in a pill inside a card is measured against the pill, not the card."""
    found = codes(svg(
        '<rect x="0" y="0" width="800" height="400"/>'
        '<rect x="40" y="40" width="90" height="30"/>'
        '<text x="44" y="60" font-size="16">Desbordamiento clarisimo</text>'
    ))
    assert "text-overflow-box" in found


# ------------------------------------------------------------ the canvas edge

def test_text_running_past_the_right_edge_is_caught():
    found = codes(svg(
        '<text x="700" y="50" font-size="16">Una etiqueta larga que se sale</text>'))
    assert "text-overflow-x" in found


def test_a_shape_outside_the_canvas_is_caught():
    assert "shape-overflow" in codes(svg('<rect x="700" y="10" width="200" height="40"/>'))


def test_a_translate_transform_is_followed():
    """Without CTM tracking this rect looks in-bounds and the overflow is missed."""
    found = codes(svg('<g transform="translate(600, 0)">'
                      '<rect x="100" y="10" width="200" height="40"/></g>'))
    assert "shape-overflow" in found


def test_a_rotate_transform_suppresses_geometry_claims():
    """Cannot follow rotate without a matrix stack, so say nothing rather than guess."""
    found = codes(svg('<g transform="rotate(45 400 200)">'
                      '<rect x="700" y="10" width="200" height="40"/></g>'))
    assert "shape-overflow" not in found


# ----------------------------------------------------- style blocks and fonts

def test_font_size_from_a_css_class_is_resolved():
    """The false-alarm regression: measuring this at the 16px default invents
    an overflow on a label that renders correctly at 13px."""
    body = (
        '<style>.sub { font-size: 13px; text-anchor: middle; }</style>'
        '<text x="400" y="56" class="sub">'
        'Dos vias para automatizar tu pyme industrial en Galicia en 2026</text>'
    )
    assert "text-overflow-x" not in codes(svg(body))


def test_a_css_class_beats_a_presentation_attribute():
    parser = mod.SvgParser()
    parser.css = mod.parse_css_classes(".big { font-size: 40px; }")
    parser.feed('<svg viewBox="0 0 800 400"><text x="10" y="50" font-size="10" '
                'class="big">X</text></svg>')
    parser.close()
    assert parser.runs[0].font_size == 40


def test_font_below_the_floor_is_rejected():
    assert "font-too-small" in codes(svg('<text x="20" y="50" font-size="11">Hola</text>'))


def test_weight_700_measures_as_600():
    """Neither site loads Inter 700; the browser falls back to 600 unwidened."""
    assert mod.text_width("Hola", 16, 700) == mod.text_width("Hola", 16, 600)


def test_combining_accents_add_no_advance():
    assert mod.text_width("á", 16, 400) == mod.text_width("a", 16, 400)


# ---------------------------------------------------------------- guardrails

@pytest.mark.parametrize("tag", ["style", "defs", "marker", "script", "linearGradient"])
def test_forbidden_elements_are_rejected(tag):
    assert "forbidden-element" in codes(svg(f"<{tag}></{tag}>"))


def test_var_in_a_geometry_attribute_is_rejected():
    assert "var-in-geometry" in codes(svg('<rect x="10" y="10" width="100" '
                                          'height="40" rx="var(--radius-md)"/>'))


def test_a_multivalue_rx_is_rejected():
    assert "multivalue-geometry" in codes(svg('<rect x="10" y="10" width="100" '
                                              'height="40" rx="8 8 0 0"/>'))


def test_an_unknown_design_token_is_rejected():
    assert "unknown-token" in codes(svg('<rect x="10" y="10" width="100" '
                                        'height="40" fill="var(--color-danger)"/>'))


def test_the_token_allowlist_is_per_stack():
    """--bg-surface exists on biglobster; the client stack calls it --bg."""
    graphic = svg('<rect x="10" y="10" width="100" height="40" fill="var(--bg-surface)"/>')
    assert "unknown-token" not in codes(graphic, "biglobster")
    assert "unknown-token" in codes(graphic, "bl-site-package")


def test_emoji_used_as_an_icon_is_rejected():
    assert "emoji-icon" in codes(svg('<text x="20" y="50" font-size="16">\U0001f4c4 Factura</text>'))


@pytest.mark.parametrize("ch", ["→", "✓", "≈", "≤", "•", "▲"])
def test_arrows_and_maths_symbols_are_not_emoji(ch):
    """A naive Extended_Pictographic match flags these and fails correct articles."""
    assert "emoji-icon" not in codes(svg(f'<text x="20" y="50" font-size="16">{ch} Total</text>'))


# -------------------------------------------------------------------- input

def test_an_infographic_is_extracted_from_a_whole_article():
    article = (
        "<article><p>Prosa.</p>"
        '<!-- infographic:auto -->'
        '<figure class="article-infographic">'
        '<svg viewBox="0 0 800 300"><text x="20" y="50" font-size="16">Hola</text></svg>'
        "<figcaption>Pie.</figcaption></figure>"
        '<!-- infographic:auto --></article>'
    )
    assert mod.extract_svgs(article)


def test_no_svg_in_the_input_exits_3(tmp_path, capsys):
    empty = tmp_path / "a.html"
    empty.write_text("<article><p>Sin grafico.</p></article>", encoding="utf-8")
    assert mod.main(["--stack", "biglobster", "--file", str(empty)]) == 3


def test_a_clean_graphic_exits_0(tmp_path):
    good = tmp_path / "a.svg"
    good.write_text(svg('<text x="20" y="50" font-size="16">Hola</text>'), encoding="utf-8")
    assert mod.main(["--stack", "biglobster", "--file", str(good), "--no-spelling"]) == 0


def test_a_defective_graphic_exits_1(tmp_path):
    bad = tmp_path / "a.svg"
    bad.write_text(svg('<rect x="700" y="10" width="200" height="40"/>'), encoding="utf-8")
    assert mod.main(["--stack", "biglobster", "--file", str(bad), "--no-spelling"]) == 1


# ------------------------------------------- spelling vs domain vocabulary ---

def test_article_vocabulary_collects_the_words_the_article_uses():
    vocab = mod.article_vocabulary("<p>El CRA obliga a toda pyme. ENISA lo supervisa.</p>")
    assert {"cra", "pyme", "enisa"} <= vocab


def test_a_term_the_article_uses_is_not_reported_as_a_misspelling(monkeypatch):
    """The regression that made the agent rewrite its own labels.

    On the first successful run hunspell flagged CRA, ENISA, pyme, dic and sep —
    all words the article itself uses — and the agent reworded the graphic to
    reach exit 0, writing "el Reglamento europeo" where a human would write "el
    CRA". A validator that changes what the content SAYS has overstepped.
    """
    monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/hunspell")

    class FakeProc:
        # hunspell -l prints one unknown word per line
        stdout = "CRA\nENISA\nqomplet\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: FakeProc())

    parser = mod.SvgParser()
    parser.feed('<svg viewBox="0 0 800 300">'
                '<text x="20" y="50" font-size="16">CRA ENISA qomplet</text></svg>')
    parser.close()

    findings = []
    mod.check_spelling(parser, findings, vocabulary={"cra", "enisa"})
    reported = [f.message for f in findings]

    assert len(reported) == 1, f"expected only the real typo, got {reported}"
    assert "qomplet" in reported[0]
    assert not any("CRA" in m or "ENISA" in m for m in reported)


def test_without_a_vocabulary_every_unknown_word_is_still_reported(monkeypatch):
    """The guard must not become a no-op: a real slip is still caught."""
    monkeypatch.setattr(mod.shutil, "which", lambda _n: "/usr/bin/hunspell")

    class FakeProc:
        stdout = "qomplet\n"

    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: FakeProc())

    parser = mod.SvgParser()
    parser.feed('<svg viewBox="0 0 800 300">'
                '<text x="20" y="50" font-size="16">qomplet</text></svg>')
    parser.close()

    findings = []
    mod.check_spelling(parser, findings, vocabulary=set())
    assert [f.code for f in findings] == ["spelling"]


# ------------------------------------------------- text printed over text

# check_text_in_box measures text against RECTS, so two labels colliding with
# each other are invisible to it — both can sit inside their boxes, or inside
# no box at all, while one word renders on top of another. That shipped on
# 2026-09-23: a "275%" set at font-size 52 overlapped the 15-unit line beside
# it by 13.8 units and the validator returned exit 0.

def test_a_label_printed_over_another_label_is_rejected():
    body = ('<text x="40" y="100" font-size="52" font-weight="700">275%</text>'
            '<text x="168" y="82" font-size="19" font-weight="700">de retorno el primer mes</text>')
    assert "text-collision" in codes(svg(body, height=300))


def test_moving_the_label_clear_of_the_number_passes():
    """The fix that shipped: start the line at x=200 instead of x=168."""
    body = ('<text x="40" y="100" font-size="52" font-weight="700">275%</text>'
            '<text x="200" y="80" font-size="19" font-weight="700">de retorno el primer mes</text>')
    assert "text-collision" not in codes(svg(body, height=300))


def test_tight_leading_is_typography_not_a_collision():
    """A label and its sub-label 16 units apart share a descender with an
    ascender. Reporting those would train everyone to skip the check — the
    same failure mode as measuring every label at the default 16px."""
    body = ('<text x="40" y="100" font-size="15" font-weight="700">Ahorro de tiempo</text>'
            '<text x="40" y="116" font-size="15">40 h/mes x 25 EUR/h</text>')
    assert "text-collision" not in codes(svg(body, height=300))


def test_labels_side_by_side_do_not_collide():
    body = ('<text x="40" y="100" font-size="15">Antes</text>'
            '<text x="400" y="100" font-size="15">Despues</text>')
    assert "text-collision" not in codes(svg(body, height=300))


# ------------------------------------------- <tspan> continuation position

# A <tspan> with no x and no dx continues at the PEN — the end of the text
# drawn so far — not back at the parent <text>'s x. Reusing the parent's x
# stacks the runs on top of each other and INVENTS collisions that do not
# render: measured against the live corpus it turned 4 real hits into 7.

def test_an_unpositioned_tspan_continues_from_the_pen():
    body = '<text x="50" y="100" font-size="13">ROI <tspan>= (beneficio / coste)</tspan></text>'
    assert "text-collision" not in codes(svg(body, height=300))


def test_a_tspan_with_its_own_x_is_positioned_absolutely():
    """The wrapping idiom the prompt recommends still measures correctly."""
    body = ('<text x="40" y="100" font-size="15">Primera linea'
            '<tspan x="40" dy="1.2em">Segunda linea</tspan></text>')
    assert "text-collision" not in codes(svg(body, height=300))


def test_a_continuation_tspan_under_a_non_start_anchor_is_left_untracked():
    """With text-anchor=middle the whole <text> is placed as a unit and the
    per-run split is not recoverable, so the run is not measured rather than
    measured wrongly — the same rule the transform handling uses."""
    parser = mod.SvgParser()
    parser.feed('<svg viewBox="0 0 800 300">'
                '<text x="400" y="100" font-size="15" text-anchor="middle">'
                'Uno <tspan>dos</tspan></text></svg>')
    parser.close()
    assert [r.tracked for r in parser.runs] == [True, False]


# ------------------------------------------------- the two-layer poster figure

# A poster is an <img> of textless artwork with the data layer laid over it. The
# two must share one coordinate system: the <svg> is positioned absolutely and
# takes its height from its own viewBox, so a mismatched image ratio separates
# them — a little at the top, badly at the bottom, and nothing looks broken.

POSTER = ('<figure class="article-infographic article-infographic--poster">'
          '<img src="/uploads/a.webp" alt="" width="800" height="400" loading="lazy">'
          '{svg}<figcaption>c</figcaption></figure>')


def poster_codes(svg_markup, img=None):
    fig = POSTER.format(svg=svg_markup)
    if img:
        fig = re.sub(r"<img\b[^>]*>", img, fig)
    return [f.code for f in mod.validate(fig, "biglobster", skip_spelling=True)]


def test_a_well_formed_poster_passes():
    assert poster_codes(svg('<text x="40" y="60" font-size="16">Hola</text>')) == []


def test_an_image_whose_ratio_differs_from_the_viewbox_is_rejected():
    codes = poster_codes(svg('<text x="40" y="60" font-size="16">Hola</text>'),
                         img='<img src="/uploads/a.webp" alt="" width="800" height="1200">')
    assert "poster-aspect-mismatch" in codes


def test_a_poster_image_needs_explicit_dimensions():
    codes = poster_codes(svg('<text x="40" y="60" font-size="16">Hola</text>'),
                         img='<img src="/uploads/a.webp" alt="">')
    assert "poster-image-unsized" in codes


def test_a_poster_image_must_have_an_empty_alt():
    """The <svg>'s title/desc is the accessible description; alt would duplicate it."""
    codes = poster_codes(svg('<text x="40" y="60" font-size="16">Hola</text>'),
                         img='<img src="/uploads/a.webp" alt="Un taller" width="800" height="400">')
    assert "poster-alt-not-empty" in codes


def test_an_img_without_the_poster_class_is_rejected():
    """Without the class neither stack overlays the layers — they stack vertically."""
    fig = ('<figure class="article-infographic">'
           '<img src="/uploads/a.webp" alt="" width="800" height="400">'
           + svg('<text x="40" y="60" font-size="16">Hola</text>')
           + "<figcaption>c</figcaption></figure>")
    assert "poster-class-missing" in [f.code for f in mod.validate(fig, "biglobster", skip_spelling=True)]


def test_the_poster_class_without_an_image_is_rejected():
    fig = ('<figure class="article-infographic article-infographic--poster">'
           + svg('<text x="40" y="60" font-size="16">Hola</text>')
           + "<figcaption>c</figcaption></figure>")
    assert "poster-image-missing" in [f.code for f in mod.validate(fig, "biglobster", skip_spelling=True)]


def test_a_plain_svg_figure_is_still_fine():
    """Not every graphic is a poster: a token-based SVG stays valid."""
    fig = ('<figure class="article-infographic">'
           + svg('<text x="40" y="60" font-size="16">Hola</text>')
           + "<figcaption>c</figcaption></figure>")
    assert [f.code for f in mod.validate(fig, "biglobster", skip_spelling=True)] == []
