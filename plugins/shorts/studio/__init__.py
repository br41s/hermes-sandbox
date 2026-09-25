"""Shorts Studio — the Remotion-quality render path for ``plugins/shorts``.

Two halves share this package:

* **Hermes side** (``package``, ``article``) validates a short's *package* —
  the script, on-screen text, visual style and social copy an agent wrote —
  and checks every figure in it against the source article before anything
  is rendered.
* **Render side** (``build`` and its helpers) runs in GitHub Actions, where
  Node, Chromium and open network are free. It turns one package into a
  finished master MP4, a Story cut, titled covers, an SRT and a QA report.

Both halves import the same ``package`` module, so the contract the agent is
held to in Hermes is byte-for-byte the one the renderer enforces. Only the
standard library is imported at module level here: the render side runs on a
bare Actions runner that never installs Hermes.

Design and setup: ``shorts/STUDIO.md``.
"""
