"""A topic rule that only lives in a prompt vanishes silently on the next rewrite.
These pin the rules that keep BigLobster's content agents on its own products."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_gap_hunter_must_name_the_product_a_topic_serves():
    text = _read("gap-hunter/biglobster-gap-hunter.prompt")
    assert "necesita <agente> porque <motivo>" in text
    assert "How to describe BigLobster" in text


def test_content_updater_skips_off_topic_posts():
    text = _read("content-updater/biglobster-content-updater.prompt")
    assert "site/_data/topics.json" in text
    assert "Skip failures and move down the queue" in text
    assert "How to describe BigLobster" in text


def test_infographic_engineer_skips_off_topic_and_noindex_posts():
    text = _read("infographic/infographic-engineer.prompt")
    assert "site/_data/topics.json" in text
    assert '"googleNoindex": true' in text


def test_infographic_engineer_report_never_attaches_media():
    # The run's checkout is deleted before delivery; a MEDIA: path in the report is
    # dropped ("not found on this host") and the run ends delivery_failed (2026-11-26).
    text = _read("infographic/infographic-engineer.prompt")
    assert "Never put a `MEDIA:` line in the report" in text
