"""Fork's own tests for tools/skill_manager_tool.py, kept out of upstream's file so upstream merges do not conflict."""

import json
from contextlib import contextmanager
from unittest.mock import patch

from tools.skill_manager_tool import _create_skill, skill_manage


@contextmanager
def _curator_pass(tmp_path, *, monkeypatch):
    """Run the body as the curator/background-review fork.

    Points HERMES_HOME at ``tmp_path/.hermes`` so skill_usage's archive path
    (``get_hermes_home()``) resolves into the same tree the skill manager
    searches, and flips ``is_background_review()`` → True so the consolidation
    guard fires.
    """
    hermes_home = tmp_path / ".hermes"
    skills_root = hermes_home / "skills"
    skills_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    with patch("tools.skill_manager_tool.SKILLS_DIR", skills_root), \
         patch("tools.skills_tool.SKILLS_DIR", skills_root), \
         patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills_root]), \
         patch("tools.skill_provenance.is_background_review", return_value=True):
        yield skills_root


def _skill_content(name: str) -> str:
    """SKILL.md whose frontmatter ``name:`` matches the directory name.

    ``skill_usage._find_skill_dir`` (used by ``archive_skill``) resolves a
    skill by its frontmatter ``name:`` field, so archive-path tests must keep
    the two in sync.
    """
    return (
        "---\n"
        f"name: {name}\n"
        "description: A test skill for unit testing.\n"
        "---\n\n"
        f"# {name}\n\n"
        "Step 1: Do the thing.\n"
    )


def _create_curated_skill(name: str, content: str = None):
    """Create a skill AND mark it curator-managed, as production does.

    ``skill_manage(action="create")`` calls ``mark_agent_created()`` when the
    background-review fork is the caller; the internal ``_create_skill()``
    helper these tests use does not. Without the marker the ownership guard
    correctly refuses every curator write, so tests exercising curator
    behaviour must set up the skill the way production leaves it.
    """
    from tools import skill_usage
    result = _create_skill(name, content if content is not None else _skill_content(name))
    skill_usage.mark_agent_created(name)
    return result


class TestStaleReadMark:
    """A write invalidates the read mark it was authorized by.

    The read-before-write guard used to track only *that* a path had been
    viewed, never *what* was viewed. One skill_view therefore authorized an
    unlimited number of later writes — including whole-file writes composed
    from the pre-write snapshot, which silently revert everything written in
    between. Observed in Langfuse trace 23f26b18…: one skill_view at 03:00:28
    stood behind three separate patches.
    """

    def test_whole_file_write_refused_after_earlier_write(self, tmp_path, monkeypatch):
        from tools.skills_tool import skill_view
        from tools.skill_manager_tool import _reset_background_review_read_marks

        _reset_background_review_read_marks()
        with _curator_pass(tmp_path, monkeypatch=monkeypatch):
            _create_curated_skill("reviewed")
            ref_dir = tmp_path / ".hermes" / "skills" / "reviewed" / "references"
            ref_dir.mkdir()
            (ref_dir / "workflow.md").write_text("one\ntwo\n", encoding="utf-8")

            assert json.loads(skill_view("reviewed", "references/workflow.md"))["success"] is True

            first = json.loads(skill_manage(
                action="patch",
                name="reviewed",
                file_path="references/workflow.md",
                old_string="one",
                new_string="ONE",
            ))
            assert first["success"] is True, first

            # The view that authorized the patch no longer describes the file,
            # so a whole-file write built from it must be refused.
            stale = json.loads(skill_manage(
                action="write_file",
                name="reviewed",
                file_path="references/workflow.md",
                file_content="one\nTWO\n",
            ))
            assert stale["success"] is False
            assert stale.get("_read_before_write_stale") is True
            assert "STALE" in stale["error"]

            # Re-reading clears it.
            assert json.loads(skill_view("reviewed", "references/workflow.md"))["success"] is True
            ok = json.loads(skill_manage(
                action="write_file",
                name="reviewed",
                file_path="references/workflow.md",
                file_content="ONE\nTWO\n",
            ))
            assert ok["success"] is True, ok
            assert (ref_dir / "workflow.md").read_text(encoding="utf-8") == "ONE\nTWO\n"

        _reset_background_review_read_marks()

    def test_anchored_patch_still_allowed_after_earlier_patch(self, tmp_path, monkeypatch):
        """patch keeps working from one read — fuzzy matching fails closed."""
        from tools.skills_tool import skill_view
        from tools.skill_manager_tool import _reset_background_review_read_marks

        _reset_background_review_read_marks()
        with _curator_pass(tmp_path, monkeypatch=monkeypatch):
            _create_curated_skill("reviewed")
            ref_dir = tmp_path / ".hermes" / "skills" / "reviewed" / "references"
            ref_dir.mkdir()
            (ref_dir / "workflow.md").write_text("alpha\nbeta\n", encoding="utf-8")

            assert json.loads(skill_view("reviewed", "references/workflow.md"))["success"] is True
            for old, new in (("alpha", "ALPHA"), ("beta", "BETA")):
                res = json.loads(skill_manage(
                    action="patch",
                    name="reviewed",
                    file_path="references/workflow.md",
                    old_string=old,
                    new_string=new,
                ))
                assert res["success"] is True, res
            assert (ref_dir / "workflow.md").read_text(encoding="utf-8") == "ALPHA\nBETA\n"

        _reset_background_review_read_marks()


class TestOverBudgetSkillRemediation:
    """An oversized SKILL.md must stay repairable.

    auditor-cron reached 100,529 chars against a 100,000 cap, at which point
    every patch — including the ones that would trim it — was rejected for
    producing oversized content. The file that agents actually load was frozen
    with wrong instructions in it while fixes piled up in references/.
    """

    def test_growing_past_cap_is_refused(self):
        from tools.skill_manager_tool import _validate_content_size, MAX_SKILL_CONTENT_CHARS

        err = _validate_content_size("x" * (MAX_SKILL_CONTENT_CHARS + 10))
        assert err is not None
        assert "over by 10" in err

    def test_shrinking_an_oversized_file_is_allowed(self):
        from tools.skill_manager_tool import _validate_content_size, MAX_SKILL_CONTENT_CHARS

        previous = "x" * (MAX_SKILL_CONTENT_CHARS + 500)
        still_over_but_smaller = "x" * (MAX_SKILL_CONTENT_CHARS + 100)
        assert _validate_content_size(still_over_but_smaller, previous=previous) is None

    def test_growing_an_already_oversized_file_is_refused(self):
        from tools.skill_manager_tool import _validate_content_size, MAX_SKILL_CONTENT_CHARS

        previous = "x" * (MAX_SKILL_CONTENT_CHARS + 100)
        bigger = "x" * (MAX_SKILL_CONTENT_CHARS + 900)
        err = _validate_content_size(bigger, previous=previous)
        assert err is not None
        assert "already over budget" in err
        assert "Trim at least 900 characters" in err

    def test_patch_that_trims_an_oversized_skill_md_succeeds(self, tmp_path, monkeypatch):
        from tools.skill_manager_tool import MAX_SKILL_CONTENT_CHARS

        skills_root = tmp_path / ".hermes" / "skills"
        skills_root.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with patch("tools.skill_manager_tool.SKILLS_DIR", skills_root), \
             patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills_root]):
            skill_dir = skills_root / "obese"
            skill_dir.mkdir()
            filler = "y" * MAX_SKILL_CONTENT_CHARS
            (skill_dir / "SKILL.md").write_text(
                _skill_content("obese") + "STALE BLOCK\n" + filler, encoding="utf-8"
            )

            # Growing it further is still refused...
            grow = json.loads(skill_manage(
                action="patch", name="obese",
                old_string="STALE BLOCK", new_string="STALE BLOCK" + "z" * 50,
            ))
            assert grow["success"] is False
            assert "already over budget" in grow["error"]

            # ...but the fix that trims it goes through.
            fix = json.loads(skill_manage(
                action="patch", name="obese",
                old_string="STALE BLOCK\n", new_string="",
            ))
            assert fix["success"] is True, fix
            assert "STALE BLOCK" not in (skill_dir / "SKILL.md").read_text(encoding="utf-8")


class TestUnrecordedSkillOwnership:
    """A skill with no usage record is not provably agent-created.

    The guard used to require a record to EXIST before refusing, so the first
    curator write against an unrecorded skill went through — and unrecorded is
    exactly the state of the manually authored skills the guard protects, since
    only mark_agent_created() (review-fork creations) ever writes the marker.
    It was self-inconsistent too: that first write calls bump_patch(), which
    creates a bare record, so every write after it was refused.
    """

    def test_first_write_to_unrecorded_skill_is_refused(self, tmp_path, monkeypatch):
        from tools.skills_tool import skill_view
        from tools.skill_manager_tool import _reset_background_review_read_marks

        _reset_background_review_read_marks()
        with _curator_pass(tmp_path, monkeypatch=monkeypatch):
            # Placed on disk directly, as a user authoring a SKILL.md would —
            # no skill_manage(create), so no usage record.
            skill_dir = tmp_path / ".hermes" / "skills" / "handwritten"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                _skill_content("handwritten"), encoding="utf-8"
            )

            assert json.loads(skill_view("handwritten"))["success"] is True
            blocked = json.loads(skill_manage(
                action="patch",
                name="handwritten",
                old_string="Step 1: Do the thing.",
                new_string="Step 1: Curated without permission.",
            ))
            assert blocked["success"] is False
            assert "no usage record" in blocked["error"]
            # Untouched on disk.
            assert "Do the thing." in (skill_dir / "SKILL.md").read_text(encoding="utf-8")

        _reset_background_review_read_marks()

    def test_agent_created_skill_is_still_curatable(self, tmp_path, monkeypatch):
        from tools.skills_tool import skill_view
        from tools.skill_manager_tool import _reset_background_review_read_marks

        _reset_background_review_read_marks()
        with _curator_pass(tmp_path, monkeypatch=monkeypatch):
            _create_curated_skill("owned")

            assert json.loads(skill_view("owned"))["success"] is True
            ok = json.loads(skill_manage(
                action="patch",
                name="owned",
                old_string="Step 1: Do the thing.",
                new_string="Step 1: Do the thing safely.",
            ))
            assert ok["success"] is True, ok

        _reset_background_review_read_marks()

    def test_foreground_writes_are_unaffected(self, tmp_path, monkeypatch):
        """The guard only fires inside the background-review fork."""
        skills_root = tmp_path / ".hermes" / "skills"
        skills_root.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        with patch("tools.skill_manager_tool.SKILLS_DIR", skills_root), \
             patch("agent.skill_utils.get_all_skills_dirs", return_value=[skills_root]):
            skill_dir = skills_root / "handwritten"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                _skill_content("handwritten"), encoding="utf-8"
            )
            ok = json.loads(skill_manage(
                action="patch",
                name="handwritten",
                old_string="Step 1: Do the thing.",
                new_string="Step 1: Edited by the user's own agent.",
            ))
            assert ok["success"] is True, ok
