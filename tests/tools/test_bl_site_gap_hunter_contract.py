"""Contracts the rented (bl-site-package) Gap Hunter prompt makes after the
2026-11-01 SEO decisions. How often it publishes is the job's schedule, chosen
per client (scripts/provision_bl_client.py --runs-per-week); what it may publish
about is the topic gate. A cron agent reads only its frozen prompt, so these pin
the wording that carries both rules."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROMPT = ROOT / "gap-hunter" / "bl-site-package-gap-hunter.prompt"


def test_one_post_per_run_and_the_schedule_sets_the_frequency():
    """Every agent CAN run daily (what we sell); the client picks the cadence.
    The prompt must not hard-code a daily or weekly cap of its own."""
    text = PROMPT.read_text(encoding="utf-8")

    assert "crear 1 artículo máximo POR EJECUCIÓN" in text
    assert "lo decide el cliente con el calendario de este job" in text
    assert "1 artículo máximo al día" not in text
    assert "A LA SEMANA" not in text


def test_the_topic_gate_names_a_category_or_service_of_the_client():
    """A rejection must name what the client sells, not just the sector: a
    sector filter is what let biglobster.top drift into HR, tax and AI news."""
    text = PROMPT.read_text(encoding="utf-8")

    assert "PUERTA DE TEMA — ¿ES DE LO QUE VENDE ESTE CLIENTE?" in text
    assert "/api/products/facets" in text
    assert "(`rechazado:tema`)" in text
    assert "Si pasa la PUERTA DE TEMA, hay un gap válido" in text
    assert "- Tema: [categoría o servicio del cliente que pasó la PUERTA DE TEMA]" in text
