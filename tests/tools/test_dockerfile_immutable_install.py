"""Contract tests for the Docker image's immutable /opt/hermes install tree."""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "Dockerfile"


def _dockerfile_text() -> str:
    return DOCKERFILE.read_text()


def _dockerfile_instructions() -> list[str]:
    """Dockerfile lines with comments and blanks stripped.

    The source-COPY block carries a comment that *quotes* the upstream
    ``COPY --link`` form it warns against, so matching raw text would find
    that prose rather than a real instruction.
    """
    return [
        line
        for line in _dockerfile_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_dockerfile_makes_opt_hermes_readonly_for_hermes_user() -> None:
    instructions = _dockerfile_instructions()
    text = _dockerfile_text()

    # --chmod on the source COPY bakes read-only perms at copy time instead
    # of a separate chmod -R pass (which walked ~30k files — #49113).
    # No `--link` here: see test_dockerfile_source_copy_omits_link below.
    assert any(
        line.strip() == "COPY --chmod=a+rX,go-w . ." for line in instructions
    ), "source COPY must bake a+rX,go-w at copy time"
    # The old tree-walking passes must not be present.
    assert "chown -R root:root /opt/hermes" not in text
    assert "chmod -R a+rX /opt/hermes" not in text
    assert "chmod -R a-w /opt/hermes" not in text


def test_dockerfile_source_copy_omits_link() -> None:
    """Guard the deliberate divergence from upstream (ebc31eb5a, 2026-07-30).

    Production images build on Cloud Build's ``gcr.io/cloud-builders/docker``,
    whose built-in ``dockerfile.v0`` frontend predates ``--link`` and fails the
    *whole build* with ``Unknown flag: link`` (build 305ad4d2). ``--chmod``
    carries the perf win and is supported there; ``--link`` only decouples the
    layer for caching, so dropping it changes nothing about image contents.

    The Dockerfile says "do NOT restore --link on the next merge", but that is
    prose. This asserts it mechanically, because an upstream merge is exactly
    how it comes back — and the failure would surface as a broken deploy, not
    a broken test.
    """
    offenders = [
        line.strip()
        for line in _dockerfile_instructions()
        if line.lstrip().startswith("COPY") and "--link" in line
    ]
    assert not offenders, (
        f"COPY --link breaks Cloud Build's dockerfile.v0 frontend: {offenders}"
    )


def test_dockerfile_does_not_chown_install_trees_to_hermes() -> None:
    text = _dockerfile_text()
    forbidden_patterns = (
        r"chown\s+-R\s+hermes:hermes\s+/opt/hermes/\.venv",
        r"chown\s+-R\s+hermes:hermes\s+/opt/hermes/ui-tui",
        r"chown\s+-R\s+hermes:hermes\s+/opt/hermes/gateway",
        r"chown\s+-R\s+hermes:hermes\s+/opt/hermes/node_modules",
    )
    for pattern in forbidden_patterns:
        assert not re.search(pattern, text), (
            "runtime install trees under /opt/hermes must stay immutable; "
            f"found forbidden pattern {pattern!r}"
        )


def test_dockerfile_bakes_code_scoped_install_method_stamp() -> None:
    """The 'docker' install-method stamp is baked next to the code.

    detect_install_method() reads the code-scoped stamp
    (/opt/hermes/.install_method) first; baking it at build time keeps the
    published image self-identifying as 'docker' WITHOUT writing into the
    shared $HERMES_HOME data volume (which a host install may also use).
    The stamp is created by root in the shim-wiring RUN block; the hermes
    user can't modify it (go-w from the --chmod on the source COPY).
    """
    text = _dockerfile_text()
    assert "printf 'docker\\n' > /opt/hermes/.install_method" in text

    # The stamp must be in the RUN block that wires the exec shim.
    shim_block = re.search(
        r"RUN mkdir -p /opt/hermes/bin && \\\n"
        r"(?:.*\\\n)+?"
        r"\s+printf 'docker\\n' > /opt/hermes/\.install_method",
        text,
    )
    assert shim_block, "install-method stamp must be in the shim-wiring RUN block"


def test_dockerfile_redirects_lazy_installs_to_durable_target() -> None:
    """Immutable image seals the venv but redirects lazy installs to the
    writable data volume, so opt-in backends still install at first use
    without being able to break the sealed core.

    Guards the contract between the Dockerfile env var, the stage2-hook
    seeding, and tools/lazy_deps.py — these three must agree on the path.
    """
    text = _dockerfile_text()
    target = "/opt/data/lazy-packages"

    # The redirect target must be set AND must live under the data volume,
    # never under the immutable /opt/hermes tree.
    assert f"ENV HERMES_LAZY_INSTALL_TARGET={target}" in text
    assert target.startswith("/opt/data/"), "target must be on the durable volume"
    assert "ENV HERMES_LAZY_INSTALL_TARGET=/opt/hermes" not in text

    # The seal flag must still be present — the redirect rides on top of it,
    # it does not replace it.
    assert "ENV HERMES_DISABLE_LAZY_INSTALLS=1" in text

    # stage2-hook must seed + chown the target dir so first-use installs
    # succeed as the unprivileged hermes runtime user.
    stage2 = (REPO_ROOT / "docker" / "stage2-hook.sh").read_text()
    assert '"$HERMES_HOME/lazy-packages"' in stage2, (
        "stage2-hook.sh must create the lazy-packages dir on the data volume"
    )
    assert "lazy-packages" in stage2.split("for sub in", 1)[1].split(";", 1)[0], (
        "lazy-packages must be in the per-boot chown subdir list so it stays "
        "hermes-owned"
    )


def test_dockerfile_bakes_photon_sidecar_deps() -> None:
    """The Photon sidecar's node_modules must be baked at build time (NS-606).

    The install tree is immutable at runtime, so a lazy `npm ci` on first
    connect would hit EROFS. Baking the deps (from the committed lockfile,
    which also runs the spectrum-ts postinstall patch) makes the hosted
    happy path install-free. Guards the contract between the Dockerfile
    and plugins/platforms/photon/sidecar_paths.resolve_sidecar_dir, which
    runs in place only when the baked deps exist and match the lockfile.
    """
    text = _dockerfile_text()

    assert "plugins/platforms/photon/sidecar/package-lock.json" in text
    assert re.search(
        r"RUN cd plugins/platforms/photon/sidecar && \\\n\s+npm ci", text
    ), "sidecar deps must be installed with `npm ci` (deterministic, runs postinstall patch)"
    # Immutability contract: never chown the sidecar tree to the runtime user.
    assert not re.search(
        r"chown\s+-R\s+hermes:hermes\s+/opt/hermes/plugins", text
    )
