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
