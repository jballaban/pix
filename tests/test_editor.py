from __future__ import annotations

from pix.editor import parse_kept_line_ids


def test_parse_extracts_line_ids_from_plan() -> None:
    text = """\
# Migration plan: F:\\src
# Run ID: 2026-05-18_18-00-00
#
# Format: L<line-id> | ACTION | path | details

L001 | TAG                | foo.jpg | event_auto null→x
L002 | RENAME             | bar.jpg | →2023-08-15_143205.jpg
L003 | DELETE             | Thumbs.db | extension policy: delete

# Summary: 0 CONVERT, 1 RENAME, 1 TAG, 1 DELETE
"""
    assert parse_kept_line_ids(text) == {"L001", "L002", "L003"}


def test_parse_skips_blank_and_comment_lines() -> None:
    text = """\
# header

# more comments

L042 | TAG | x.jpg | a

# Summary: ...
"""
    assert parse_kept_line_ids(text) == {"L042"}


def test_parse_handles_user_deleted_lines() -> None:
    """Deleting L002 in the editor must leave L001 and L003 in the kept set."""
    text = """\
L001 | TAG | a.jpg | x
L003 | RENAME | b.jpg | →c.jpg
"""
    assert parse_kept_line_ids(text) == {"L001", "L003"}


def test_parse_empty_plan_returns_empty_set() -> None:
    text = "# Just comments\n# And more\n"
    assert parse_kept_line_ids(text) == set()


def test_parse_supports_annotated_lines() -> None:
    """During apply, plan lines acquire [Started]/[Completed] suffixes."""
    text = """\
L001 | TAG | a.jpg | x    [14:32:01 Completed]
L002 | RENAME | b.jpg | →c.jpg    [14:32:04 Started]
"""
    assert parse_kept_line_ids(text) == {"L001", "L002"}


def test_parse_supports_double_digit_line_ids() -> None:
    text = "L100 | TAG | x.jpg | a\nL999 | DELETE | y.jpg | b\n"
    assert parse_kept_line_ids(text) == {"L100", "L999"}
