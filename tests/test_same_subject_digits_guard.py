"""The guard of numbers of the review of subjects (memory_core 6.20.0,
`objects.numbers_of`): by VALUE, read before any normalization, the same for
every script, no list of digits. Two names whose numbers differ are never
offered to each other; the same number in two scripts is one number."""
import pytest

from korely_agent.services.memory_core import objects as O
from tests.subject_world import Port, fact, memory, vec


@pytest.mark.parametrize("a,b", [
    ("二号楼", "三号楼"),            # han numerals
    ("Rocky Ⅱ", "Rocky Ⅲ"),        # Roman numerals as characters
    ("፩ ቤት", "፪ ቤት"),              # Ethiopic
    ("Cloud 1.0.30", "Cloud"),     # a version and the product
    ("Cloud 1.0.30", "Cloud 1.0.35"),
    ("Studio ½", "Studio"),
], ids=["han", "roman", "ethiopic", "version-and-product", "two-versions", "a-fraction"])
def test_different_numbers_are_never_offered(a, b):
    assert not O.same_numbers(a, b)


@pytest.mark.parametrize("a,b", [
    ("مبنى ١٥", "مبنى 15"),          # Arabic-Indic digits and ASCII: one value
    ("Ｃｌｏｕｄ １", "Cloud 1"),      # full-width digits
    ("Cloud di Korely", "Korely Cloud"),
], ids=["arabic-indic", "full-width", "no-number"])
def test_the_same_value_in_another_script_passes(a, b):
    assert O.same_numbers(a, b)


def test_a_run_of_digits_is_one_number_read_positionally():
    assert O.numbers_of("v2 build 15") == (2, 15)
    assert O.numbers_of("١٥") == (15,)


def test_numbers_in_words_and_latin_roman_numerals_are_letters():
    """The declared limit: "two" and "II" are letters, and the model reads them."""
    assert O.numbers_of("Rocky II") == ()
    assert O.numbers_of("version two") == ()


def test_the_review_never_offers_a_version_to_its_product(db, tenant):
    m = memory(db, tenant, "Cloud runs. Cloud 1.0.30 was released.")
    fact(db, tenant, "Cloud", "runs_on", "Hetzner", mem=m, minutes_ago=300, vector=vec(1))
    fact(db, tenant, "Cloud 1.0.30", "released_on", "2026-11-02", mem=m, minutes_ago=200, vector=vec(1))
    from korely_agent.services.facts import subjects

    r = subjects.review(db, tenant.id, None, dry_run=True)
    assert r["subjects"] == 2
    assert r["pairs"] == [], "the guard of numbers took the pair out before the shortlist"
    assert Port().calls == []
