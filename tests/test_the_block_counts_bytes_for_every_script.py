"""The context block is packed by UTF-8 bytes, four to a token (18 Oct 2026).

Four characters to a token is a Latin-alphabet figure: a block in Chinese,
Japanese or Korean passed its budget by up to three times (the audit of
17 Oct 2026). Measured on 16 languages with gpt-oss's tokenizer: with bytes the
worst is 1.20 (zh), English within 3 points of before.
"""
from korely_agent.services import context


def test_english_weighs_what_it_weighed():
    text = "Maria moved to Milan in March and started at Globex. " * 10
    assert context._estimate_tokens(text) == len(text) // 4


def test_a_cjk_text_weighs_its_bytes():
    text = "水は生命にとって欠かせない。" * 20
    # Three bytes each: three times what four characters a token said.
    assert context._estimate_tokens(text) == (3 * len(text)) // 4


def test_a_fragment_fits_its_bytes_and_ends_on_a_character():
    text = "水は生命にとって欠かせない。" * 40
    frag = context._truncate_to_budget(text, 50)
    assert frag and len(frag.encode("utf-8")) <= 50 * 4 - 2
    frag.encode("utf-8")  # a whole character at the end: no half of one
    assert text.startswith(frag.rstrip("…"))


# Texts the cut finds no ". " and no space in: a long token, a URL, a script
# written without spaces. The fragment ends with the 3-byte "…", which until
# the audit of 19 Oct 2026 was paid on top of the room: the second take of
# _pack_memories failed and the most relevant memory left the block with
# every one after it, in 3 rooms of 4 for these texts.
_UNSPACED = (
    "x" * 2000,
    "https://example.com/" + "a1b2c3d4" * 250,
    "水は生命にとって欠かせない。" * 120,
)


def test_a_fragment_ending_with_the_ellipsis_fits_its_budget():
    for text in _UNSPACED:
        for budget in range(11, 120):  # below, no fragment is worth keeping
            frag = context._truncate_to_budget(text, budget)
            assert frag.endswith("…"), (text[:10], budget)
            assert context._cost(frag) <= budget * context.BYTES_PER_TOKEN - 2, (text[:10], budget)
            assert text.startswith(frag[:-1])


def test_the_top_memory_is_kept_as_a_fragment_at_every_remainder_of_four():
    # The two takes of _pack_memories, as it makes them, at each room modulo
    # BYTES_PER_TOKEN: the fragment's line must fit where the whole did not.
    for text in _UNSPACED:
        line = f"- {text}"
        for room in range(400, 400 + 2 * context.BYTES_PER_TOKEN):
            for overhead in (1, 1 + 40):
                pack = context._Packer(1000)
                pack.used = pack.cap - room
                assert not pack.take(overhead + context._cost(line))
                frag = context._truncate_to_budget(
                    line[2:], (pack.room() - overhead) // context.BYTES_PER_TOKEN)
                assert frag, (text[:10], room, overhead)
                assert pack.take(overhead + context._cost(f"- {frag}")), (text[:10], room, overhead)
