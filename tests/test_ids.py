"""The public ids. `mem_...`, `fct_...`, `job_...`, `aud_...`.

Small enough that it looked like it did not need tests, which is why it did not
have any. It sits on the published contract: every id a client stores, logs or
pastes into a support ticket comes through here, and the two ways it can break
are both silent. A parser that accepts the wrong kind of id looks up a UUID
that is simply not found, and a parser that raises turns a typo in somebody's
curl into a 500.
"""
import uuid

from korely_agent.services import ids


class TestRoundTrip:
    def test_every_kind_survives(self):
        u = uuid.uuid4()
        assert ids.parse_mem_id(ids.mem_public_id(u)) == u
        assert ids.parse_fact_id(ids.fact_public_id(u)) == u
        assert ids.parse_job_id(ids.job_public_id(u)) == u

    def test_the_prefix_says_what_it_is(self):
        u = uuid.uuid4()
        assert ids.mem_public_id(u).startswith("mem_")
        assert ids.fact_public_id(u).startswith("fct_")
        assert ids.audit_public_id(u).startswith("aud_")
        assert ids.job_public_id(u).startswith("job_")

    def test_no_dashes_in_the_published_form(self):
        """`.hex`, not `str()`. A dashed id in a URL is not wrong, but the
        contract published one shape and clients match on it."""
        s = ids.mem_public_id(uuid.uuid4())
        assert "-" not in s
        assert len(s) == len("mem_") + 32

    def test_it_does_not_care_how_the_uuid_arrives(self):
        u = uuid.uuid4()
        assert ids.mem_public_id(u) == ids.mem_public_id(str(u)) == ids.mem_public_id(u.hex)


class TestParsing:
    def test_a_bare_uuid_still_works(self):
        """Documented: a client that round-trips the raw id keeps working."""
        u = uuid.uuid4()
        assert ids.parse_mem_id(str(u)) == u
        assert ids.parse_mem_id(u.hex) == u

    def test_the_wrong_kind_of_id_is_refused(self):
        """A fact id where a memory id belongs must not decode. If it did, the
        lookup would miss and the caller would be told the memory does not
        exist, which sends them looking in the wrong place."""
        u = uuid.uuid4()
        assert ids.parse_mem_id(ids.fact_public_id(u)) is None
        assert ids.parse_fact_id(ids.mem_public_id(u)) is None
        assert ids.parse_job_id(ids.mem_public_id(u)) is None

    def test_rubbish_is_none_and_never_an_exception(self):
        for bad in ("", "   ", "mem_", "mem_nothex", "not-an-id", "mem_" + "z" * 32,
                    "mem_mem_" + uuid.uuid4().hex, "../../etc/passwd", "1", "null"):
            assert ids.parse_mem_id(bad) is None, bad

    def test_none_is_none(self):
        assert ids.parse_mem_id(None) is None
        assert ids.parse_fact_id(None) is None
        assert ids.parse_job_id(None) is None

    def test_a_type_that_is_not_a_string(self):
        """FastAPI hands these strings, but a caller inside the codebase can
        pass whatever it has. None rather than a traceback."""
        for bad in (123, 4.5, [], {}, object()):
            assert ids.parse_mem_id(bad) is None
