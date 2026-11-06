from debeloglav.bot import DISCORD_LIMIT, split


def test_short_text_is_one_chunk():
    assert split("živjo") == ["živjo"]


def test_empty_text_has_no_chunks():
    assert split("") == []


def test_long_text_splits_at_lines_within_the_limit():
    lines = [f"vrstica {i} " + "x" * 80 for i in range(60)]
    chunks = split("\n".join(lines))
    assert len(chunks) > 1
    assert all(len(c) <= DISCORD_LIMIT for c in chunks)
    assert "\n".join(chunks).split() == "\n".join(lines).split()


def test_text_without_spaces_is_cut_hard():
    chunks = split("x" * (DISCORD_LIMIT * 2 + 5))
    assert [len(c) for c in chunks] == [DISCORD_LIMIT, DISCORD_LIMIT, 5]
