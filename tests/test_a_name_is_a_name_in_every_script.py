"""A name is a name in every script (memory_core 6.21.0, `identity.valid_name`).

Until 6.21.0 a name was `^[^\\W\\d_][\\w .\\-&'/+]{0,59}$`: Python's `\\w` takes
letters and digits but no combining mark, so every name of the scripts whose
vowel signs, virama or points are marks (Devanagari, Bengali, Tamil, Thai,
Hebrew with its points, Arabic with its vowels) was "not a name", and no
alias, no link of two objects and no review of two subjects could ever
start from one: "इंफोसिस" / "Infosys" never reached the writer (the blind set
of 23 Nov 2026: 54 true pairs of 263 kept out by this rule, every Devanagari
one among them).

What the rule exists for stays out: sentences, lists, dates, versions,
times, URLs, email addresses, disambiguation in parentheses, invisible
characters. A comma is part of a name only where a person declares it
(`valid_declared_name`); where the engine decides by itself (a rename, a
link, a review) there is no comma, until that is measured. No model.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from korely_agent.services.memory_core import identity as I
from korely_agent.services.memory_core import objects as O

UNICODE = Path(__file__).parent / "data" / "unicode"

# Names the rule of before 6.21.0 refused, and that are names.
NAMES_OF_EVERY_SCRIPT = [
    "इंफोसिस",                      # Devanagari: a vowel sign (Mc) and the anusvara (Mn)
    "टाटा मोटर्स",                   # Devanagari, with the virama
    "भारतीय रिज़र्व बैंक",              # Devanagari, with the nukta
    "বাংলাদেশ ব্যাংক",                 # Bengali
    "தமிழ்நாடு",                     # Tamil
    "ไทยแอร์เวย์",                    # Thai: its vowels and tone marks are marks
    "שָׁלוֹם",                       # Hebrew with its points
    "צה״ל",                    # Hebrew gershayim, inside an abbreviation
    "القُدْس",                       # Arabic with its vowels
    "カ\u3099ス",            # katakana with a combining voiced mark (ガス, decomposed)
    "ニューヨーク・タイムズ",            # katakana middle dot between the words of a foreign name
    "پژوهش\u200cگاه",               # Persian: ZWNJ between two joining letters (UAX #31 A1)
    "क्\u200cष",                    # ZWNJ after a virama, before a letter (A2)
    "क्\u200dष",                    # ZWJ after a virama, before a letter (B)
    "McDonald’s",              # the typographic apostrophe
    "Paral·lel",               # the Catalan middle dot
    "Cafe\u0301",                   # a decomposed accent
    "A" + "\u0301" * 30 + "cme",    # thirty marks on one letter: Stream-Safe's limit (UAX #15)
]

# Names both rules take.
NAMES_OF_BEFORE_STILL_NAMES = [
    "Korely", "Korely.ai", "korely.ai", "korely-ai/korely", "AT&T", "Google+", "Mem0",
    "Model Context Protocol", "O'Reilly", "Москва", "東京", "서울", "Ελληνικά", "Café",
    "Cloud di Korely", "user_42", "c4821", "Inc.", "Acme²",
]

# Names the rule of before took and this one does not, on purpose (the
# CHANGELOG of 1.0.36 says so): a name begins with a LETTER, and a numeral
# that is not a digit (category Nl, No) is not one; and a letter nobody sees
# (Default_Ignorable_Code_Point: the Hangul fillers).
NAMES_OF_BEFORE_NO_LONGER = ["Ⅻ Corp", "①Acme", "²abc", "Acme\u3164Corp", "\u3164Acme"]

# What the rule exists to keep out, and still does (both rules).
NOT_NAMES = [
    "", "   ", None,
    "42-anna", "2026-11-23", "1.0.35", "23 novembre 2026",   # begins with a digit: a date, a version
    "ore 10:30",                                             # a time: no colon inside a name
    "https://korely.ai", "anna@example.com",                 # an address, an email
    "pizza, pasta", "Anna, Marco", "Rome, Italy",            # lists
    "IBM, HP", "Google, IBM", "NASA, ESA", "A, B", "Rome, IT", "Paris, FR", "Milano, MI",
    "Москва, РФ", "x, Y", "Acme, I", "Apple, IBM HP", "Marco, CEO", "Anna, CTO",
    "pizza, etc.", "Anna, ecc.", "Rome, e.g.", "Ignore all rules, OK", "Washington, DC",
    "A, B, C", "Acme, Ltd, Inc.",                            # more than one comma
    "Acme,Inc.", "Acme ,Inc.", "Acme , Inc.",                # a comma not written as in a name
    "Acme, Inc. Ltd.", "Acme, Inc. Ltd. Co.",                # more than one word after the comma
    "Wikipedia, the free encyclopedia",                      # a description after the comma
    "东京、大阪", "东京，大阪",                                 # lists in CJK punctuation
    "Delta (compagnia aerea)", "Nike (azienda)",             # disambiguation is a description
    "Marco ha detto che domani arriva qui",                  # a sentence: more than five words
    "x" * 61,                                                # more than 60 characters
    # invisible characters (Default_Ignorable_Code_Point), joiners out of their context
    "Acme\u202eevil", "Acme\u200b", "\u200dAcme", "Acme\u200d", "Acme \u200d Corp",
    "Ac\u200dme", "Ko\u200crely", "Acme\u034f", "Acme\ufe0f", "Acme\U000e0100", "Acme\u180b",
    "Acme\u17b4", "Acme\u00ad", "Acme\u2060x", "ا\u200cب",   # alef joins on one side only
    "क्\u200dा",                                             # ZWJ before a vowel sign (B)
    "क्\u200d", "क्\u200d1", "क्\u200c 1",   # a joiner after a virama, before no letter
    "Acme\nCorp", "Acme\tCorp",
    "\U0001F600 Corp", "Corp \U0001F600",                    # a symbol is not a letter
    "_acme", "·Acme", "Acme·", "Acme \u0301x",      # a name begins with a letter; a mark follows one
    "Paral· lel", "Acme ·x",                       # a middle dot between two letters only
    "Korely․ai", "Korely﹒ai", "Korely．ai", "Acme‧x", "Acme·x",  # dots like a full stop
    "A" + "\u0301" * 31 + "cme",                             # more marks than a letter takes
    "A\u20dd", "Acme1\u20e3",                                # enclosing marks are a symbol's
]

# Names a person may declare, and the engine never makes by itself.
DECLARED_NAMES = ["Airbnb, Inc.", "Telefónica, S.A.", "Meta Platforms, Inc.", "Washington, D.C.",
                  "Barilla, S.p.A."]


@pytest.mark.parametrize("name", NAMES_OF_EVERY_SCRIPT)
def test_a_name_of_any_script_is_a_name(name):
    assert I.valid_name(name) and I.valid_declared_name(name)


@pytest.mark.parametrize("name", NAMES_OF_BEFORE_STILL_NAMES)
def test_a_name_of_before_is_still_a_name(name):
    assert I.valid_name(name) and I.valid_declared_name(name)


@pytest.mark.parametrize("name", NAMES_OF_BEFORE_NO_LONGER, ids=[repr(n) for n in NAMES_OF_BEFORE_NO_LONGER])
def test_a_numeral_or_an_invisible_letter_first_is_no_longer_a_name(name):
    assert not I.valid_name(name) and not I.valid_declared_name(name)


@pytest.mark.parametrize("name", NOT_NAMES, ids=[repr(n)[:40] for n in NOT_NAMES])
def test_what_is_not_a_name_stays_out(name):
    assert not I.valid_name(name)
    assert not I.valid_declared_name(name)


@pytest.mark.parametrize("name", DECLARED_NAMES)
def test_a_comma_name_is_a_name_only_where_a_person_declares_it(name):
    assert I.valid_declared_name(name)
    assert not I.valid_name(name)


def test_a_name_is_read_as_written_around_its_spaces():
    """The name is stripped before it is read, as before 6.21.0."""
    assert I.valid_declared_name("  Airbnb, Inc.  ")
    assert I.valid_name(" इंफोसिस ")


def test_a_hindi_rename_makes_an_alias_and_a_comma_name_does_not():
    """The engine's own alias: an explicit rename in Devanagari makes one now;
    one into a comma name does not, as no comma enters what the engine decides
    by itself."""
    assert I.alias_rule_from_identity("renamed_to", "पुराना नाम", "नया नाम", subject_type="product") == (
        "पुराना नाम", "नया नाम")
    assert I.alias_rule_from_identity("renamed_to", "Square", "Block, Inc.", subject_type="organization") is None


def test_a_declared_name_follows_the_rule_of_its_source():
    """By hand a comma name is a name; from a rename fact it is not."""
    args = dict(existing_source=None, existing_canonical=None)
    assert I.new_alias_outcome({}, "इंफोसिस", "Infosys", source=I.SOURCE_USER, **args) == ("created", "Infosys")
    assert I.new_alias_outcome({}, "Airbnb", "Airbnb, Inc.", source=I.SOURCE_USER, **args) == (
        "created", "Airbnb, Inc.")
    assert I.new_alias_outcome({}, "Airbnb", "Airbnb, Inc.", source=I.SOURCE_IDENTITY_FACT, **args) == (
        "invalid", None)


@pytest.mark.parametrize("a, b", [
    ("Cafe\u0301", "Café"),                     # decomposed and composed
    ("\u095b\u0930\u093e", "\u091c\u093c\u0930\u093e"),   # a nukta letter in one character and in two
    ("ＡＣＭＥ", "acme"),                         # full width
    ("STRASSE", "Straße"),                     # case folding
])
def test_two_unicode_forms_of_one_name_are_one_key(a, b):
    """The key of an alias and of a link is the judge's (`name_key`): one name,
    one key, so no alias of a name to itself."""
    assert I.name_key(a) == I.name_key(b) == O.key_of(a) == O.key_of(b)
    assert I.new_alias_outcome({}, a, b, source=I.SOURCE_USER, existing_source=None,
                               existing_canonical=None) == ("same", None)


def test_an_alias_map_finds_a_name_in_another_unicode_form():
    m = {I.name_key("Café"): "Coffee Shop"}
    assert I.canonical_of("Cafe\u0301", m) == "Coffee Shop"


def _ranges_of(path: Path, want: str) -> set:
    cps = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        cp, prop = [x.strip() for x in line.split(";")[:2]]
        if prop == want:
            a, _, b = cp.partition("..")
            cps.update(range(int(a, 16), int(b or a, 16) + 1))
    return cps


@pytest.mark.parametrize("table, path, prop", [
    ("_DEFAULT_IGNORABLE", "DefaultIgnorable-14.0.0.txt", "Default_Ignorable_Code_Point"),
    ("_JOINS_BOTH", "DerivedJoiningType-14.0.0.txt", "D"),
    ("_JOINS_LEFT", "DerivedJoiningType-14.0.0.txt", "L"),
    ("_JOINS_RIGHT", "DerivedJoiningType-14.0.0.txt", "R"),
])
def test_the_tables_are_the_unicode_consortiums(table, path, prop):
    """Every code point, against the Unicode Character Database 14.0.0."""
    expected = _ranges_of(UNICODE / path, prop)
    ranges = getattr(I, table)
    assert ranges == tuple(sorted(ranges))
    got = {c for a, b in ranges for c in range(a, b + 1)}
    assert got == expected


def test_the_transparent_letters_are_the_unicode_consortiums():
    import unicodedata

    t = _ranges_of(UNICODE / "DerivedJoiningType-14.0.0.txt", "T")
    assert {c for c in t if unicodedata.category(chr(c)) not in ("Mn", "Me", "Cf")} == set(I._TRANSPARENT_LETTERS)


def test_the_lookup_of_a_table_is_exact_at_its_edges():
    for table in (I._DEFAULT_IGNORABLE, I._JOINS_BOTH, I._JOINS_LEFT, I._JOINS_RIGHT):
        for a, b in table:
            for cp in (a - 1, a, b, b + 1):
                if 0 <= cp <= 0x10FFFF and not 0xD800 <= cp <= 0xDFFF:
                    assert I._in(table, chr(cp)) == any(x <= cp <= y for x, y in table)


def test_a_name_in_devanagari_can_be_declared_for_an_end_user(db, tenant):
    """The door that answered 422 to every Devanagari name: POST
    /v1/users/{id}/names creates it now (201), as it creates "Anna Rossi"; a
    list stays a 422."""
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app
    from korely_agent.services import keys as key_service

    _row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="names")
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    headers = {"Authorization": f"Bearer {full}"}
    with TestClient(app) as client:
        hindi = client.post("/v1/users/u-hindi/names", headers=headers, json={"name": "इंफोसिस"})
        legal = client.post("/v1/users/u-legal/names", headers=headers, json={"name": "Airbnb, Inc."})
        listed = client.post("/v1/users/u-list/names", headers=headers, json={"name": "IBM, HP"})
    app.dependency_overrides.clear()
    assert hindi.status_code == 201, hindi.text
    assert hindi.json()["name"] == "इंफोसिस"
    assert legal.status_code == 201, legal.text
    assert listed.status_code == 422, listed.text


def test_an_alias_written_before_is_found_by_its_old_key(db, tenant):
    """No row is rewritten: an alias row of before keeps the alias lowercased
    as its key, and a write after finds it by either key (`store.alias_keys`)."""
    from korely_agent.db.models import AgentEntityAlias
    from korely_agent.services.facts import entities as ent
    from korely_agent.services.memory_core import store

    row = AgentEntityAlias(tenant_id=tenant.id, project_id=None, end_user_id="u-1", agent_id=None,
                           alias_lower="straße", alias_label="Straße", canonical_label="Main Street",
                           source=I.SOURCE_USER, confidence=1.0)
    db.add(row)
    db.commit()
    mem = ent.memory_of(tenant.id, None, "u-1", None)
    assert store.memory_alias_map(db, ent.TABLES, mem) == {"strasse": "Main Street"}
    again, outcome = store.add_alias(db, ent.TABLES, mem, "STRASSE", "Main Street", source=I.SOURCE_USER,
                                     recanonicalize_now=False)
    assert outcome == "same" and again.id == row.id
    assert db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant.id).count() == 1


# ---------------------------------------------------------------------------
# the reads and writes of the database under the key of 6.21.0
# ---------------------------------------------------------------------------

def _store_world():
    from korely_agent.db.models import AgentEntityAlias, AgentFact, AgentMemory
    from korely_agent.services.facts import entities as ent
    from korely_agent.services.memory_core import store
    return AgentEntityAlias, AgentFact, AgentMemory, ent, store


def _fact(db, tenant, subject, user, project_id=None):
    import uuid
    from datetime import datetime, timezone

    from korely_agent.core.llm import EMBEDDING_DIM
    _A, AgentFact, AgentMemory, _ent, _store = _store_world()
    m = AgentMemory(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project_id, end_user_id=user,
                    content="x", embedding=[0.1] * EMBEDDING_DIM, graph_sync_status="done")
    db.add(m)
    db.commit()
    f = AgentFact(id=uuid.uuid4(), tenant_id=tenant.id, project_id=project_id, end_user_id=user,
                  subject_label=subject, subject_canonical=subject, predicate_raw="uses",
                  predicate_canonical="uses", object_label="Slack", object_canonical="Slack",
                  source_memory_id=m.id, source_memory_ids=[str(m.id)],
                  valid_from=datetime(2026, 3, 1, tzinfo=timezone.utc))
    db.add(f)
    db.commit()
    return f.id


def _found(db, tenant, name, user, project_id=None):
    """How many facts the read by name finds (`entities.name_condition`, the
    read of GET /v1/facts?subject=)."""
    from sqlalchemy import func
    _A, AgentFact, _M, ent, _s = _store_world()
    sc = func.lower(func.coalesce(AgentFact.subject_canonical, AgentFact.subject_label))
    cond = ent.name_condition(db, tenant.id, name, sc, project_id=project_id, end_user_id=user,
                              all_end_users=False, agent_id=None)
    return db.query(AgentFact.id).filter(AgentFact.tenant_id == tenant.id, cond).count()


CHAINS = [
    ("ascii", "GY", "Gordon", "Acme"),
    ("sharp-s", "GX", "Straße", "Korely"),
    ("decomposed", "GZ", "Cafe\u0301", "Coffee Co"),
    ("full-width", "GW", "\uff27\uff4f\uff52\uff44\uff4f\uff4e", "Acme Two"),
]


@pytest.mark.parametrize("label, first, middle, last", CHAINS, ids=[c[0] for c in CHAINS])
def test_a_chain_through_a_name_of_any_form_is_flattened_and_read(db, tenant, label, first, middle, last):
    """GX -> Straße, then Straße -> Korely: GX points at Korely, and a read by
    any of the three names finds the fact. The flattening read the database's
    lower() of the middle name, which is not its key: the chain stayed GX ->
    Straße and the read by GX found nothing (the review of 23 Nov 2026)."""
    AgentEntityAlias, _F, _M, ent, store = _store_world()
    mem = ent.memory_of(tenant.id, None, "u-chain", None)
    _fact(db, tenant, first, "u-chain")
    store.add_alias(db, ent.TABLES, mem, first, middle, source=I.SOURCE_USER)
    store.add_alias(db, ent.TABLES, mem, middle, last, source=I.SOURCE_USER)
    db.commit()
    row = db.query(AgentEntityAlias).filter(AgentEntityAlias.tenant_id == tenant.id,
                                           AgentEntityAlias.alias_label == first).one()
    assert row.canonical_label == last
    assert {n: _found(db, tenant, n, "u-chain") for n in (first, middle, last)} == {first: 1, middle: 1, last: 1}


@pytest.mark.parametrize("label, first, middle, last", CHAINS, ids=[c[0] for c in CHAINS])
def test_undoing_the_middle_of_a_chain_of_any_form_puts_it_back(db, tenant, label, first, middle, last):
    AgentEntityAlias, _F, _M, ent, store = _store_world()
    mem = ent.memory_of(tenant.id, None, "u-undo", None)
    store.add_alias(db, ent.TABLES, mem, first, middle, source=I.SOURCE_USER)
    store.add_alias(db, ent.TABLES, mem, middle, last, source=I.SOURCE_USER)
    db.commit()
    A = AgentEntityAlias
    middle_row = next(r for r in db.query(A).filter(A.tenant_id == tenant.id) if I.name_key(r.alias_label) == I.name_key(middle))
    store.remove_alias(db, ent.TABLES, mem, middle_row)
    db.commit()
    row = db.query(A).filter(A.tenant_id == tenant.id, A.alias_label == first).one()
    assert I.name_key(row.canonical_label) == I.name_key(middle)


def test_a_row_of_before_and_a_row_of_now_are_both_read(db, tenant):
    """A row of before keeps the alias lowercased as its key ("straße"), a row
    written now `name_key` ("strasse"): a read by name finds the facts of
    each, under any spelling of the name."""
    AgentEntityAlias, _F, _M, ent, store = _store_world()
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=None, end_user_id="u-old", agent_id=None,
                            alias_lower="straße", alias_label="Straße", canonical_label="Main Street",
                            source=I.SOURCE_USER, confidence=1.0))
    db.commit()
    _fact(db, tenant, "Main Street", "u-old")
    assert _found(db, tenant, "Straße", "u-old") == 1
    mem = ent.memory_of(tenant.id, None, "u-new", None)
    row, outcome = store.add_alias(db, ent.TABLES, mem, "Straße", "High Street", source=I.SOURCE_USER)
    db.commit()
    assert outcome == "created" and row.alias_lower == "strasse"
    _fact(db, tenant, "High Street", "u-new")
    assert _found(db, tenant, "Straße", "u-new") == 1
    assert _found(db, tenant, "STRASSE", "u-new") == 1


def test_a_name_of_before_declared_again_is_no_new_name_for_the_cap(db, tenant, monkeypatch):
    AgentEntityAlias, _F, _M, ent, _s = _store_world()
    monkeypatch.setattr(ent, "MAX_DECLARED_NAMES", 1)
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=None, end_user_id="u-cap", agent_id=None,
                            alias_lower="straße", alias_label="Straße", canonical_label="u-cap",
                            source=I.SOURCE_USER, confidence=1.0))
    db.commit()
    mem = ent.memory_of(tenant.id, None, "u-cap", None)
    ent._room_for_a_name(db, mem, tenant.id, "Straße")     # the same name, found by its old key: no new one
    with pytest.raises(ent.TooManyNames):
        ent._room_for_a_name(db, mem, tenant.id, "Another Name")


def test_a_name_held_for_another_in_another_unicode_form_is_a_409(db, tenant):
    from fastapi.testclient import TestClient

    from korely_agent.db import get_db
    from korely_agent.main import create_app
    from korely_agent.services import keys as key_service
    _A, _F, _M, ent, store = _store_world()
    key_row, full = key_service.create_api_key(db, tenant_id=tenant.id, name="names")
    project = key_service.key_project(db, key_row)
    store.add_alias(db, ent.TABLES, ent.memory_of(tenant.id, project, "u-b", None), "Café", "Bistro",
                    source=I.SOURCE_USER)
    db.commit()
    app = create_app()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as client:
        r = client.post("/v1/users/u-b/names", headers={"Authorization": f"Bearer {full}"},
                        json={"name": "Cafe\u0301"})
    app.dependency_overrides.clear()
    assert r.status_code == 409, r.text


def test_a_moved_alias_meets_the_target_row_under_its_new_key(db, tenant):
    """A project's deletion moves its aliases into the default project: a row
    of before ("straße") meets the target's row of now ("strasse") as one
    name, and a different decision there is not overwritten."""
    import uuid

    from korely_agent.db.models import AgentProject
    from korely_agent.services import projects
    AgentEntityAlias, _F, _M, ent, store = _store_world()
    p1 = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="one", slug="one")
    p2 = AgentProject(id=uuid.uuid4(), tenant_id=tenant.id, name="two", slug="two")
    db.add_all([p1, p2])
    db.commit()
    store.add_alias(db, ent.TABLES, ent.memory_of(tenant.id, p2.id, "u-m", None), "Straße", "Main Street",
                    source=I.SOURCE_USER)
    db.add(AgentEntityAlias(tenant_id=tenant.id, project_id=p1.id, end_user_id="u-m", agent_id=None,
                            alias_lower="straße", alias_label="Straße", canonical_label="High Street",
                            source=I.SOURCE_USER, confidence=1.0))
    db.commit()
    assert projects._move_aliases(db, tenant.id, p1.id, p2.id) == (0, 1)
    db.commit()
    A = AgentEntityAlias
    there = db.query(A).filter(A.tenant_id == tenant.id, A.project_id == p2.id, A.end_user_id == "u-m").all()
    assert [r.canonical_label for r in there] == ["Main Street"]


def test_undoing_a_chain_of_rows_of_before_puts_it_back(db, tenant):
    """Rows of before keep their names lowercased as keys ("straße"); the
    undo reads them under `name_key`, as the map does: removing the middle of
    GX -> Straße -> Korely points GX at Straße again."""
    AgentEntityAlias, _F, _M, ent, store = _store_world()
    common = dict(tenant_id=tenant.id, project_id=None, end_user_id="u-old-chain", agent_id=None,
                  source=I.SOURCE_USER, confidence=1.0)
    gx = AgentEntityAlias(alias_lower="gx", alias_label="GX", canonical_label="Korely",
                          stated_canonical="Straße", **common)
    middle = AgentEntityAlias(alias_lower="straße", alias_label="Straße", canonical_label="Korely",
                              stated_canonical="Korely", **common)
    db.add_all([gx, middle])
    db.commit()
    store.remove_alias(db, ent.TABLES, ent.memory_of(tenant.id, None, "u-old-chain", None), middle)
    db.commit()
    db.refresh(gx)
    assert gx.canonical_label == "Straße"



@pytest.mark.parametrize("look_alike, plain", [("\u0410\u0441me", "Acme"), ("K\u03bfrely", "Korely"), ("L\u0131nux", "Linux")])
def test_letters_that_look_alike_in_two_scripts_are_another_name(look_alike, plain):
    """What the CHANGELOG's Security line says: the rule keeps out invisible
    characters, not letters of another script that look the same. Such a name
    is a name, and another name than the one it looks like."""
    assert I.valid_name(look_alike)
    assert I.name_key(look_alike) != I.name_key(plain)


def _two_rows_of_before(db, tenant, user, *, older, newer):
    """Two alias rows of before whose keys meet under `name_key` ("straße" and
    "strasse"), the NEWER one inserted first and given the smaller id, so that
    neither the order of the table nor the order of the ids is the order of
    creation: only `created_at` says which is the older."""
    import uuid
    from datetime import datetime, timezone

    AgentEntityAlias, _F, _M, _ent, _s = _store_world()
    common = dict(tenant_id=tenant.id, project_id=None, end_user_id=user, agent_id=None,
                  source=I.SOURCE_USER, confidence=1.0)
    new_row = AgentEntityAlias(id=uuid.UUID(int=1), alias_lower="strasse", alias_label="STRASSE",
                               created_at=datetime(2026, 10, 2, tzinfo=timezone.utc), **newer, **common)
    db.add(new_row)
    db.commit()
    old_row = AgentEntityAlias(id=uuid.UUID(int=2 ** 128 - 1), alias_lower="straße", alias_label="Straße",
                               created_at=datetime(2026, 10, 1, tzinfo=timezone.utc), **older, **common)
    db.add(old_row)
    db.commit()
    return old_row, new_row


def test_the_map_reads_the_oldest_row_before_and_after_an_update(db, tenant):
    """Of two rows of before under one key the map reads the oldest, and an
    alias declared again updates that same row: the map then reads the new
    canonical. Ordered by `since`, which the update sets to now, the updated
    row went last and the map read the other one (the review of 23 Nov
    2026)."""
    _A, _F, _M, ent, store = _store_world()
    _two_rows_of_before(db, tenant, "u-order", older=dict(canonical_label="Xco"), newer=dict(canonical_label="Yco"))
    mem = ent.memory_of(tenant.id, None, "u-order", None)
    assert store.memory_alias_map(db, ent.TABLES, mem) == {"strasse": "Xco"}
    row, outcome = store.add_alias(db, ent.TABLES, mem, "Straße", "Zco", source=I.SOURCE_USER)
    db.commit()
    assert outcome == "updated" and row.alias_lower == "straße"
    assert store.memory_alias_map(db, ent.TABLES, mem) == {"strasse": "Zco"}


def test_the_undo_reads_the_oldest_of_two_rows_of_before(db, tenant):
    """The undo computes the chains from what each row stated, the oldest row
    of a key first, as the map: removing the name's row puts GX back on
    Straße."""
    AgentEntityAlias, _F, _M, ent, store = _store_world()
    old_row, _new_row = _two_rows_of_before(
        db, tenant, "u-undo-two", older=dict(canonical_label="Korely", stated_canonical="Korely"),
        newer=dict(canonical_label="Qco", stated_canonical="Qco"))
    gx = AgentEntityAlias(tenant_id=tenant.id, project_id=None, end_user_id="u-undo-two", agent_id=None,
                          alias_lower="gx", alias_label="GX", canonical_label="Korely", stated_canonical="Straße",
                          source=I.SOURCE_USER, confidence=1.0)
    db.add(gx)
    db.commit()
    store.remove_alias(db, ent.TABLES, ent.memory_of(tenant.id, None, "u-undo-two", None), old_row)
    db.commit()
    db.refresh(gx)
    assert gx.canonical_label == "Straße"


def test_a_flattened_chain_is_in_the_database_before_the_commit(db, tenant):
    """The rows the flattening rewrites are written before anything reads the
    table again in the same transaction (the facts' recanonicalization reads
    it), not only at the commit."""
    AgentEntityAlias, _F, _M, ent, store = _store_world()
    mem = ent.memory_of(tenant.id, None, "u-flush", None)
    store.add_alias(db, ent.TABLES, mem, "GX", "Straße", source=I.SOURCE_USER, recanonicalize_now=False)
    store.add_alias(db, ent.TABLES, mem, "Straße", "Korely", source=I.SOURCE_USER, recanonicalize_now=False)
    A = AgentEntityAlias
    assert db.query(A.canonical_label).filter(A.tenant_id == tenant.id, A.alias_label == "GX").scalar() == "Korely"
    db.commit()
