"""Tests for revenue import customer name matching logic.

Tests the progressive word-level prefix matching system that links revenue CSV
customer names to NoteHelper customers.
"""
import pytest

from app.services.revenue_import import (
    _clean_for_matching,
    _progressive_word_prefix_match,
    _get_acronym,
    _build_customer_lookup,
    _resolve_customer_id,
)


class TestCleanForMatching:
    """Tests for _clean_for_matching helper."""

    def test_lowercases(self):
        assert _clean_for_matching("NOVOPATH") == "novopath"

    def test_strips_commas(self):
        assert _clean_for_matching("Azara Healthcare, LLC") == "azara healthcare llc"

    def test_strips_periods(self):
        assert _clean_for_matching("DanubeNet, Inc.") == "danubenet inc"

    def test_collapses_whitespace(self):
        assert _clean_for_matching("  Foo   Bar  ") == "foo bar"

    def test_empty(self):
        assert _clean_for_matching("") == ""


class TestProgressiveWordPrefixMatch:
    """Tests for _progressive_word_prefix_match."""

    def test_exact_match(self):
        assert _progressive_word_prefix_match("novopath", "novopath") is True

    def test_a_is_word_prefix_of_b(self):
        """Revenue name words are a prefix of customer name words."""
        assert _progressive_word_prefix_match(
            "ltc consulting", "ltc consulting services"
        ) is True

    def test_b_is_word_prefix_of_a(self):
        """Customer name words match after dropping trailing words."""
        assert _progressive_word_prefix_match(
            "azara healthcare llc", "azara healthcare"
        ) is True

    def test_streamline_health_matches_via_word_drop(self):
        """'streamline health' does NOT word-match 'streamline healthcare'
        (health != healthcare), but after dropping 'health', 'streamline'
        matches the first word of 'streamline healthcare solutions'."""
        assert _progressive_word_prefix_match(
            "streamline health", "streamline healthcare solutions"
        ) is True

    def test_signature_healthcare(self):
        assert _progressive_word_prefix_match(
            "signature healthcare llc", "signature healthcare"
        ) is True

    def test_danubenet(self):
        assert _progressive_word_prefix_match(
            "danubenet inc", "danubenet"
        ) is True

    def test_no_match(self):
        assert _progressive_word_prefix_match("microsoft", "apple") is False

    def test_empty_strings(self):
        assert _progressive_word_prefix_match("", "something") is False
        assert _progressive_word_prefix_match("something", "") is False

    def test_too_short_no_match(self):
        """Single words shorter than 4 chars shouldn't match."""
        assert _progressive_word_prefix_match("ab", "abc") is False

    def test_stop_words_skipped(self):
        """Stop words at the end should be skipped when dropping words."""
        assert _progressive_word_prefix_match(
            "acme solutions the", "acme solutions"
        ) is True

    def test_american_is_skip_word(self):
        """'american' is a skip word so generic it shouldn't be matched alone.
        After dropping 'express', 'american' is skipped, no match."""
        assert _progressive_word_prefix_match(
            "american express", "american airlines"
        ) is False

    def test_single_word_too_short(self):
        """Single word that's < 4 chars should NOT match."""
        assert _progressive_word_prefix_match("ace", "acme") is False

    def test_single_word_exact_match(self):
        """Single word exact match with >= 4 chars should match."""
        assert _progressive_word_prefix_match("danubenet", "danubenet inc") is True

    def test_no_partial_word_match(self):
        """'north' should NOT match 'northern' — word-level, not char-level."""
        assert _progressive_word_prefix_match(
            "north ohio medical", "northern ohio medical specialists"
        ) is False


class TestGetAcronym:
    """Tests for _get_acronym helper."""

    def test_basic_acronym(self):
        assert _get_acronym("Facilities Survey Inc") == "FSI"

    def test_skips_stop_words(self):
        """Skip words like 'of', 'the', 'and' shouldn't contribute letters."""
        assert _get_acronym("Bank of America") == "BA"

    def test_single_word(self):
        assert _get_acronym("Novopath") == "N"

    def test_empty_string(self):
        assert _get_acronym("") == ""

    def test_strips_punctuation(self):
        assert _get_acronym("DanubeNet, Inc.") == "DI"

    def test_all_stop_words(self):
        """If all words are stop words, result is empty."""
        assert _get_acronym("a the of") == ""

    def test_short_words_skipped(self):
        """Single-char words are skipped (< 2 chars)."""
        assert _get_acronym("A B Company") == "C"


class TestBuildCustomerLookup:
    """Tests for _build_customer_lookup with real database."""

    def test_returns_tuple(self, app):
        with app.app_context():
            result = _build_customer_lookup()
            assert isinstance(result, tuple)
            assert len(result) == 3

    def test_exact_name_in_lookup(self, app):
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(name="Test Corp", tpid=12345)
            db.session.add(customer)
            db.session.commit()

            exact_lookup, _, _ = _build_customer_lookup()
            assert "test corp" in exact_lookup
            assert exact_lookup["test corp"] == customer.id

    def test_nickname_in_lookup(self, app):
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(
                name="Long Company Name", nickname="LCN",
                tpid=12345
            )
            db.session.add(customer)
            db.session.commit()

            exact_lookup, _, _ = _build_customer_lookup()
            assert "lcn" in exact_lookup

    def test_cleaned_names_populated(self, app):
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(name="Megacorp", tpid=99999)
            db.session.add(customer)
            db.session.commit()

            _, cleaned_names, _ = _build_customer_lookup()
            found = [cn for cn in cleaned_names if cn[1] == customer.id]
            assert len(found) >= 1
            assert found[0][0] == "megacorp"

    def test_acronym_lookup_populated(self, app):
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(name="Facilities Survey Inc", tpid=55555)
            db.session.add(customer)
            db.session.commit()

            _, _, acronym_lookup = _build_customer_lookup()
            assert "FSI" in acronym_lookup
            assert acronym_lookup["FSI"] == customer.id


class TestResolveCustomerId:
    """Tests for _resolve_customer_id end-to-end."""

    def test_exact_match(self, app):
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(name="Azara Healthcare", tpid=111)
            db.session.add(customer)
            db.session.commit()

            exact, cleaned, acronyms = _build_customer_lookup()
            assert _resolve_customer_id(exact, cleaned, "Azara Healthcare", acronyms) == customer.id

    def test_exact_nickname_match(self, app):
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(
                name="Long Name Here", nickname="LNH",
                tpid=222
            )
            db.session.add(customer)
            db.session.commit()

            exact, cleaned, acronyms = _build_customer_lookup()
            assert _resolve_customer_id(exact, cleaned, "LNH", acronyms) == customer.id

    def test_suffix_match_llc(self, app):
        """'Azara Healthcare, LLC' matches 'Azara Healthcare' via prefix."""
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(name="Azara Healthcare", tpid=333)
            db.session.add(customer)
            db.session.commit()

            exact, cleaned, acronyms = _build_customer_lookup()
            result = _resolve_customer_id(exact, cleaned, "Azara Healthcare, LLC", acronyms)
            assert result == customer.id

    def test_prefix_match_ltc(self, app):
        """'LTC CONSULTING' matches 'LTC Consulting Services' via prefix."""
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(
                name="LTC Consulting Services", tpid=444
            )
            db.session.add(customer)
            db.session.commit()

            exact, cleaned, acronyms = _build_customer_lookup()
            result = _resolve_customer_id(exact, cleaned, "LTC CONSULTING", acronyms)
            assert result == customer.id

    def test_streamline_health(self, app):
        """'STREAMLINE HEALTH' matches 'Streamline Healthcare Solutions'."""
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(
                name="Streamline Healthcare Solutions", tpid=555
            )
            db.session.add(customer)
            db.session.commit()

            exact, cleaned, acronyms = _build_customer_lookup()
            result = _resolve_customer_id(exact, cleaned, "STREAMLINE HEALTH", acronyms)
            assert result == customer.id

    def test_no_match_returns_none(self, app):
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(name="Acme Corp", tpid=777)
            db.session.add(customer)
            db.session.commit()

            exact, cleaned, acronyms = _build_customer_lookup()
            result = _resolve_customer_id(
                exact, cleaned, "Totally Different Company XYZ", acronyms
            )
            assert result is None

    def test_case_insensitive(self, app):
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(name="NOVOPATH", tpid=888)
            db.session.add(customer)
            db.session.commit()

            exact, cleaned, acronyms = _build_customer_lookup()
            assert _resolve_customer_id(exact, cleaned, "Novopath", acronyms) == customer.id

    def test_acronym_match_fsi(self, app):
        """'FSI' matches 'Facilities Survey Inc' via acronym."""
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(name="Facilities Survey Inc", tpid=999)
            db.session.add(customer)
            db.session.commit()

            exact, cleaned, acronyms = _build_customer_lookup()
            result = _resolve_customer_id(exact, cleaned, "FSI", acronyms)
            assert result == customer.id

    def test_acronym_match_case_insensitive(self, app):
        """Acronym matching should be case-insensitive on input."""
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(name="Facilities Survey Inc", tpid=1001)
            db.session.add(customer)
            db.session.commit()

            exact, cleaned, acronyms = _build_customer_lookup()
            result = _resolve_customer_id(exact, cleaned, "fsi", acronyms)
            assert result == customer.id

    def test_acronym_reverse_match(self, app):
        """Customer named 'FSI' matches CSV name 'Facilities Survey Inc' via acronym."""
        from app.models import db, Customer
        with app.app_context():
            customer = Customer(name="FSI", tpid=1002)
            db.session.add(customer)
            db.session.commit()

            exact, cleaned, acronyms = _build_customer_lookup()
            result = _resolve_customer_id(
                exact, cleaned, "Facilities Survey Inc", acronyms
            )
            assert result == customer.id
