"""Identity + reaction-state caches (pure)."""

from miniteams.directory import Directory, _mri_from_userlink


def test_reaction_diff_tracks_add_and_remove(directory: Directory) -> None:
    added, removed = directory.reaction_diff("m", "like", ["u1", "u2"])
    assert added == {"u1", "u2"}
    assert removed == set()
    added, removed = directory.reaction_diff("m", "like", ["u2", "u3"])
    assert added == {"u3"}
    assert removed == {"u1"}


def test_name_for_uses_cache_then_strips_prefix(directory: Directory) -> None:
    directory.note_name("8:orgid:guid", "Alice")
    assert directory.name_for("8:orgid:guid") == "Alice"
    assert directory.name_for("8:orgid:unknown") == "unknown"


def test_note_name_ignores_empty(directory: Directory) -> None:
    directory.note_name("", "x")
    directory.note_name("8:o:u", None)
    assert directory.name_for("8:o:u") == "u"


def test_mri_from_userlink() -> None:
    assert _mri_from_userlink("https://h/v1/users/8:orgid:g") == "8:orgid:g"
    assert _mri_from_userlink(None) == ""


async def test_label_special_thread_no_network(directory: Directory) -> None:
    assert await directory.label("48:notes") == "Notes to self"
