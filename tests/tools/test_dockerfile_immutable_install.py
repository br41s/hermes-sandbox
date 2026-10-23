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


def test_dockerfile_leaves_pm_installs_on_for_the_data_volume() -> None:
    """Upstream v0.22 retired the lazy-install redirect: the venv stays sealed and PM
    installs opt-in dependencies into generations under $HERMES_HOME (/opt/data/installs).

    The fork depends on that path being open: Langfuse left core for the ``langfuse``
    catalog plugin, which the left-core migration installs at gateway start. An image
    that sets HERMES_DISABLE_LAZY_INSTALLS again (the pre-v0.22 seal) turns tracing off
    on every profile with only a warning in agent.log.
    """
    text = _dockerfile_text()
    assert "HERMES_DISABLE_LAZY_INSTALLS" not in "\n".join(_dockerfile_instructions())
    assert "ENV HERMES_HOME=/opt/data" in text
    # The sealed core still lives outside the data volume.
    assert "--sealed" in text and "--out /opt/hermes/.venv" in text


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
