"""Contracts the BigLobster Gap Hunter prompt makes with files and a ledger it
cannot check itself: a cron agent reads its frozen prompt and nothing else, so a
rule that counts ledger lines or reads an approved file is only as good as the
shapes the prompt names. These pin those shapes against the real ones."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROMPT = ROOT / "gap-hunter" / "biglobster-gap-hunter.prompt"
TEMPLATE = ROOT / "gap-hunter" / "first-party-facts.template.md"

# A day's closing line exactly as the live ledger holds it (2026-10-31).
LEDGER = """\
2026-10-24 | ciencia | https://arxiv.org/abs/2610.00001 | escrito | a paper
2026-10-24 | RESULTADO | - | escrito | articulo /blog/a-post.html + cover
2026-10-26 | RESULTADO | - | sin-accion | nada paso las puertas
2026-10-30 | RESULTADO | - | escrito | articulo /blog/b-post.html + cover
2026-10-31 | estudio | https://example.org/study | escrito | a study
2026-10-31 | RESULTADO | - | escrito | articulo /blog/c-post.html + cover
"""


def _weekly_cap_command() -> str:
    text = PROMPT.read_text(encoding="utf-8")
    match = re.search(r"`(DESDE=\$\(date[^`]*?wc -l)`", text)
    assert match, "FASE 1B must give the exact command that counts the week's posts"
    return match.group(1)


@pytest.mark.parametrize(
    ("since", "expected"),
    [("2026-10-25", 2), ("2026-10-31", 1), ("2026-10-01", 3)],
)
def test_the_weekly_count_reads_only_closing_lines_that_wrote(tmp_path, since, expected):
    """A candidate row also says `escrito`; counting it would double every post
    and trip the cap at half the volume."""
    ledger = tmp_path / "research-ledger.md"
    ledger.write_text(LEDGER, encoding="utf-8")
    command = _weekly_cap_command()
    command = re.sub(r"DESDE=\$\(date[^)]*\)", f"DESDE={since}", command).replace("<libro>", str(ledger))

    out = subprocess.run(["sh", "-c", command], capture_output=True, text=True, check=True).stdout

    assert int(out) == expected


def test_the_prompt_tells_the_agent_to_write_the_line_the_count_reads():
    text = PROMPT.read_text(encoding="utf-8")

    assert "YYYY-MM-DD | RESULTADO | - | escrito | articulo /blog/<slug>.html" in text


def test_the_shipped_first_party_template_is_not_approved():
    """Copying the template unchanged must never put a file in front of the
    hunter: approval is a deliberate edit by the owner."""
    head = TEMPLATE.read_text(encoding="utf-8").split("---")[1]

    assert re.search(r"^aprobado:\s*no\b", head, re.MULTILINE)
    assert re.search(r"^clientes-redactados:\s*no\b", head, re.MULTILINE)


def test_every_repo_file_the_prompt_names_exists():
    text = PROMPT.read_text(encoding="utf-8")
    named = set(re.findall(r"`(gap-hunter/[\w.-]+)`", text))

    assert "gap-hunter/first-party-facts.template.md" in named
    assert not [p for p in named if not (ROOT / p).exists()]
