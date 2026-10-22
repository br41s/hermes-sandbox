"""scripts/geo_exclusiones.sh (seeded by docker/cont-init.d/03-biglobster-config) and the
GEO Scout's STEP 0.5: SocialBot's exclusion list reaches the scout fail-closed.

The script is run for real against a fake ``buzz`` on PATH: a dated canvas is copied into
the scout's workdir silently; an undated canvas, a failing ``buzz`` or a missing channel
deletes any old copy and exits 2, so the runner alerts and the brief says NO VERIFICADO.
"""

import os
import re
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "docker" / "cont-init.d" / "03-biglobster-config"
PROMPT = ROOT / "offsite-geo" / "geo-scout.prompt"
DEST = Path("profiles/biglobster/geo-scout/salida/socialbot/exclusiones.md")

LIST = """# Exclusiones de SocialBot para el GEO Scout
actualizado: 2026-10-22

## Plataformas
- hacker news (news.ycombinator.com) | excluido | siempre | regla de Brais
"""


def _script(tmp_path: Path) -> Path:
    text = INIT.read_text(encoding="utf-8")
    body = re.search(r"<<'GEOEXCL'\n(.*?)\nGEOEXCL\n", text, re.S)
    assert body, "geo_exclusiones.sh heredoc not found in 03-biglobster-config"
    script = tmp_path / "geo_exclusiones.sh"
    script.write_text(body.group(1) + "\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _fake_buzz(tmp_path: Path, output: str, code: int = 0) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "canvas.md").write_text(output, encoding="utf-8")
    buzz = bindir / "buzz"
    buzz.write_text(f"#!/bin/bash\ncat '{tmp_path / 'canvas.md'}'\nexit {code}\n", encoding="utf-8")
    buzz.chmod(buzz.stat().st_mode | stat.S_IEXEC)
    return bindir


def _run(tmp_path: Path, output: str, code: int = 0, channel: str = "42d4e93e", stale: bool = True):
    home = tmp_path / "home"
    dest = home / DEST
    if stale:
        dest.parent.mkdir(parents=True)
        dest.write_text("old list\n", encoding="utf-8")
    env = dict(os.environ, HERMES_HOME=str(home), PATH=f"{_fake_buzz(tmp_path, output, code)}:{os.environ['PATH']}")
    env.pop("SHORTS_BUZZ_CHANNEL", None)
    if channel:
        env["SHORTS_BUZZ_CHANNEL"] = channel
    result = subprocess.run(["bash", str(_script(tmp_path))], capture_output=True, text=True, env=env, timeout=30)
    return result, dest


def test_a_dated_canvas_is_copied_silently(tmp_path):
    result, dest = _run(tmp_path, LIST)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""                      # empty stdout = silent success for a no-agent job
    assert dest.read_text(encoding="utf-8") == LIST
    assert not dest.with_suffix(".md.tmp").exists()


def test_an_undated_canvas_deletes_the_old_copy_and_alerts(tmp_path):
    result, dest = _run(tmp_path, LIST.replace("actualizado: 2026-10-22\n", ""))
    assert result.returncode == 2
    assert not dest.exists()
    assert "NO VERIFICADO" in result.stderr


def test_a_failing_buzz_deletes_the_old_copy_and_alerts(tmp_path):
    result, dest = _run(tmp_path, LIST, code=1)
    assert result.returncode == 2
    assert not dest.exists()


def test_without_the_shorts_channel_nothing_is_trusted(tmp_path):
    result, dest = _run(tmp_path, LIST, channel="")
    assert result.returncode == 2
    assert not dest.exists()


def test_the_scout_reads_only_the_canvas_copy_fail_closed():
    prompt = PROMPT.read_text(encoding="utf-8")
    step = prompt.split("STEP 0.5", 1)[1].split("STEP 1 ", 1)[0]
    assert "salida/socialbot/exclusiones.md" in step
    assert "actualizado: YYYY-MM-DD" in step and "7 days" in step
    assert "exclusiones NO VERIFICADO" in step and "exclusiones ok" in step
    # The old two-file contract read a section estado.md does not have: an empty set and a false "ok".
    assert "estado.md" not in prompt and "comunidades.md" not in prompt
