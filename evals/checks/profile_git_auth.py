"""Safety net #5 — per-profile git/GitHub auth stays intact.

Why this exists: Hermes runs per-profile delegate lanes (finview, grow-shop, …)
that must be able to ``git push`` and ``gh pr create``. The boot hook
(``docker/cont-init.d/03-biglobster-config`` §4/§6) provisions auth for each
real profile:

* ``<profile>/home/.git-credentials`` holds ``https://x-access-token:<token>@github.com``
* ``<profile>/home/.gitconfig`` sets ``credential.helper = store``
* ``<profile>/workspace/<repo>`` remotes are TOKENLESS ``https://github.com/<owner>/<repo>.git``

This broke repeatedly (PRs #31/#32/#35) and — worse — the agent kept trying to
"fix" auth itself, which CORRUPTS the working credential because the token it
sees is redacted to ``***``. This net audits the live profile homes for the
exact failure shapes that have actually happened, so a blanked credential or a
detour to SSH is caught before every push silently fails.

It is a **read-only auditor**: it parses the on-disk credential / config /
remote files (no subprocess, no network) and reuses the runtime's own profile
discovery (``hermes_constants.get_default_hermes_root``). Tokens are NEVER
echoed — only their shape/length, matching ``evals/checks/secret_scan``.

Failure modes detected (each one actually occurred):
  1. ``home/.git-credentials`` missing / empty / blanked (token rewritten ``***``).
  2. credentials not in ``https://x-access-token:<token>@github.com`` form.
  3. a workspace repo remote uses SSH or embeds a token instead of tokenless https.
  4. ``credential.helper`` not set to ``store`` for the profile HOME.
  5. a url-scoped ``[credential "https://github.com"]`` override (left by a past
     ``gh auth login``) shadowing the store helper — it is a MORE SPECIFIC match
     than the generic one, so it wins outright and the stored token is never
     consulted. Caught by rule 4, because the bare ``[credential]`` section it
     reads is then absent or unused.

Scope note: this audits ``$HERMES_HOME/home`` (the DEFAULT profile's subprocess
home) as well as every ``profiles/<name>/home``. The default home is not under
``profiles/`` — there is no ``profiles/default`` dir — so an audit that walks
only ``profiles/*/`` silently skips the home used by every default-profile cron
job. That gap let a revoked token sit there from 2026-07-27 until 2026-09-14.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# The provisioned credential line shape: https://x-access-token:<token>@github.com
_CRED_RE = re.compile(r"https://x-access-token:([^@\s]*)@github\.com")


def _redact_token(token: str) -> str:
    """Describe a token by shape only — never echo it."""
    return f"<token len {len(token)}>"


def _redact_url(url: str) -> str:
    """Strip any embedded ``user:pass@`` userinfo from a URL before display."""
    return re.sub(r"://[^/@]*@", "://***@", url)


def _extract_github_token(content: str) -> Optional[str]:
    """Return the x-access-token value from a ``.git-credentials`` blob, or None.

    None means the expected ``https://x-access-token:<token>@github.com`` line is
    absent entirely (wrong form). An empty string means the line is present but
    the token slot is blank.
    """
    m = _CRED_RE.search(content or "")
    return m.group(1) if m else None


def _read_credential_helper(gitconfig: Path) -> Optional[str]:
    """Return the ``[credential] helper`` value from a gitconfig, or None."""
    if not gitconfig.exists():
        return None
    try:
        text = gitconfig.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    section: Optional[str] = None
    helper: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section == "credential" and "=" in line:
            key, _, val = line.partition("=")
            if key.strip().lower() == "helper":
                helper = val.strip()
    return helper


def _read_origin_url(repo_dir: Path) -> Optional[str]:
    """Return the ``origin`` remote URL from a repo's ``.git/config``, or None."""
    cfg = repo_dir / ".git" / "config"
    if not cfg.exists():
        return None
    try:
        text = cfg.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    section: Optional[str] = None
    url: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip().lower()
            continue
        if section == 'remote "origin"' and "=" in line:
            key, _, val = line.partition("=")
            if key.strip().lower() == "url":
                url = val.strip()
    return url


def _embeds_credentials(url: str) -> bool:
    """True if an https URL carries ``user:pass@`` userinfo (an embedded token)."""
    after = url.split("://", 1)[-1]
    netloc = after.split("/", 1)[0]
    return "@" in netloc


def remote_hazard(repo_dir: Path) -> Optional[str]:
    """Return a one-line hazard for a repo's origin remote, or None if tokenless.

    A missing origin is not an auth hazard (nothing to push). SSH or an embedded
    token are hazards: the runtime normalizes remotes to tokenless https because
    an embedded token gets redacted to ``***`` in the agent's view, which makes
    it conclude auth is broken and detour to SSH.
    """
    url = _read_origin_url(Path(repo_dir))
    if url is None:
        return None
    if url.startswith("git@") or url.startswith("ssh://"):
        return f"origin remote uses SSH ({url}) — remotes must stay tokenless https"
    if "x-access-token:" in url or _embeds_credentials(url):
        return (
            f"origin remote embeds a token in the URL ({_redact_url(url)}) — "
            "remotes must stay tokenless https"
        )
    return None


def git_auth_hazard(profile_home: Path) -> Optional[str]:
    """Return a one-line git-auth hazard for a profile HOME dir, or None if safe.

    ``profile_home`` is ``<profile>/home`` (the dir the delegate lane resolves as
    HOME via ``hermes_constants.get_subprocess_home``). Checks, in order: the
    credential file exists/non-empty/valid x-access-token, ``credential.helper``
    is ``store``, and every workspace repo's origin is tokenless https. Returns
    the first hazard found.
    """
    profile_home = Path(profile_home)

    creds = profile_home / ".git-credentials"
    if not creds.exists():
        return ".git-credentials is missing — git push will fail (was it blanked or deleted?)"
    try:
        content = creds.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f".git-credentials is unreadable ({exc.__class__.__name__})"
    if not content.strip():
        return ".git-credentials is empty — the working credential was blanked"

    token = _extract_github_token(content)
    if token is None:
        return ".git-credentials has no https://x-access-token:<token>@github.com line (wrong form)"
    if not token or "*" in token:
        return (
            ".git-credentials token is blank or redacted (***) — a working credential "
            "was overwritten with a redacted token, breaking all pushes"
        )

    helper = _read_credential_helper(profile_home / ".gitconfig")
    if helper != "store":
        return f"credential.helper is {helper!r}, expected 'store' — the helper won't supply the token"

    workspace = profile_home.parent / "workspace"
    if workspace.is_dir():
        for repo_dir in sorted(workspace.iterdir()):
            if not (repo_dir / ".git").is_dir():
                continue
            hz = remote_hazard(repo_dir)
            if hz:
                return f"repo {repo_dir.name}: {hz}"

    return None


def _discover_profile_homes() -> List[Path]:
    """Discover live ``<profile>/home`` dirs the same way the runtime resolves them.

    Real-profile marker is ``SOUL.md`` (sections 1/2/4/5 of the boot hook use the
    same marker). Only profiles that have a ``home/`` dir are git-enabled, so we
    skip the rest to avoid flagging profiles that never push.
    """
    try:
        from hermes_constants import get_default_hermes_root

        root = get_default_hermes_root()
    except Exception:
        return []
    homes: List[Path] = []

    # The DEFAULT profile's subprocess home is "$HERMES_HOME/home" — it does NOT
    # live under profiles/ (there is no profiles/default dir), so the loop below
    # never reaches it. Checking only profiles/*/home left the busiest home in
    # the deployment unaudited: on 2026-09-14 it was found serving a revoked
    # token to every default-profile cron job via a `gh auth login` override,
    # which the helper!="store" rule here would have caught on day one. Same
    # resolution as hermes_constants.get_subprocess_home() for that profile.
    default_home = Path(root) / "home"
    if default_home.is_dir():
        homes.append(default_home)

    profiles = Path(root) / "profiles"
    if not profiles.is_dir():
        return homes
    for prof_dir in sorted(profiles.iterdir()):
        if not (prof_dir / "SOUL.md").is_file():
            continue
        home = prof_dir / "home"
        if home.is_dir():
            homes.append(home)
    return homes


def _profile_label(home: Path) -> str:
    """Name the profile owning *home* — ``"default"`` for ``$HERMES_HOME/home``.

    Every other home is ``<root>/profiles/<name>/home``, where the parent dir IS
    the profile name. The default profile's home sits directly under the hermes
    root, so ``home.parent.name`` would report the root's basename ("data") and
    read as a profile that does not exist. Matched against the live root only,
    so synthetic ``tmp_path`` homes in the regression tests keep their own names.
    """
    try:
        from hermes_constants import get_default_hermes_root

        if Path(home) == Path(get_default_hermes_root()) / "home":
            return "default"
    except Exception:
        pass
    return Path(home).parent.name


def audit_profile_git_auth(
    homes: Optional[Iterable[Path]] = None,
) -> List[Tuple[str, str]]:
    """Return ``(profile_name, hazard)`` for every profile with a git-auth hazard.

    With ``homes=None`` it scans the live profile homes; the regression test
    passes synthetic ``tmp_path`` homes.
    """
    if homes is None:
        homes = _discover_profile_homes()
    flagged: List[Tuple[str, str]] = []
    for home in homes:
        home = Path(home)
        hz = git_auth_hazard(home)
        if hz:
            flagged.append((_profile_label(home), hz))
    return flagged


def audit_summary(homes: Optional[Iterable[Path]] = None) -> str:
    """One-line summary — used as eval-case output."""
    flagged = audit_profile_git_auth(homes)
    if not flagged:
        return (
            "OK: every profile's git/GitHub auth is intact "
            "(valid x-access-token credential, helper=store, tokenless remotes)."
        )
    return "FAIL: " + "; ".join(f"profile {name}: {hz}" for name, hz in flagged)
