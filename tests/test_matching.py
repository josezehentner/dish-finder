import pytest

from config import SNIPPET_RADIUS
from matching import Document, Hit, find_hits, normalize, search


class TestNormalize:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("TAFELSPITZ", "tafelspitz"),
            ("Käsespätzle", "kaesespaetzle"),
            ("Öl Übel", "oel uebel"),
            ("Weißbier", "weissbier"),
            ("Café Crème", "cafe creme"),
            ("Club\n\t  Mate", "club mate"),
        ],
    )
    def test_normalizes(self, raw, expected):
        assert normalize(raw) == expected

    def test_decomposed_umlaut_is_spelled_out(self):
        # "a" + combining diaeresis (NFD), as some PDFs store it
        assert normalize("Spätzle") == "spaetzle"

    def test_umlaut_and_spelled_out_form_are_equal(self):
        assert normalize("Kaspressknödel") == normalize("KASPRESSKNOEDEL")


class TestFindHits:
    def test_finds_term_regardless_of_case_and_umlauts(self):
        hits = find_hits("Heute: KASPRESSKNOEDEL in Rindsuppe", ["Kaspressknödel"], "https://x.at", "html")

        assert hits == [
            Hit(
                term="Kaspressknödel",
                url="https://x.at",
                kind="html",
                count=1,
                snippet="heute: kaspressknoedel in rindsuppe",
            )
        ]

    def test_counts_all_occurrences(self):
        hits = find_hits("Mate, Mate und nochmal Mate", ["mate"], "u", "html")
        assert hits[0].count == 3

    def test_returns_one_hit_per_matching_term_only(self):
        hits = find_hits("Club Mate und Bier", ["Club Mate", "Mate", "Wein"], "u", "pdf")
        assert [h.term for h in hits] == ["Club Mate", "Mate"]
        assert all(h.kind == "pdf" for h in hits)

    def test_no_match_returns_empty_list(self):
        assert find_hits("Schnitzel mit Salat", ["Tafelspitz"], "u", "html") == []

    def test_substring_match_by_default(self):
        assert find_hits("Tomatensuppe", ["mate"], "u", "html")

    def test_whole_word_skips_substrings(self):
        assert find_hits("Tomatensuppe", ["mate"], "u", "html", whole_word=True) == []
        assert find_hits("Eine Mate, bitte", ["mate"], "u", "html", whole_word=True)

    def test_regex_characters_in_term_are_literal(self):
        assert find_hits("Toast (vegan)", ["(vegan)"], "u", "html")
        assert find_hits("Toast vegan", ["(vegan)"], "u", "html") == []

    def test_snippet_is_cut_around_first_match(self):
        text = "a" * 500 + " tafelspitz " + "b" * 500
        snippet = find_hits(text, ["Tafelspitz"], "u", "html")[0].snippet

        assert "tafelspitz" in snippet
        assert len(snippet) <= len("tafelspitz") + 2 * SNIPPET_RADIUS


class TestSearch:
    def test_searches_all_documents_in_order(self):
        documents = [
            Document("https://a.at", "html", "Tafelspitz"),
            Document("https://a.at/karte", "html", "nur Schnitzel"),
            Document("https://a.at/karte.pdf", "pdf", "Tafelspitz und Club Mate"),
        ]

        hits = search(documents, ["Tafelspitz", "Club Mate"])

        assert [(h.url, h.kind, h.term) for h in hits] == [
            ("https://a.at", "html", "Tafelspitz"),
            ("https://a.at/karte.pdf", "pdf", "Tafelspitz"),
            ("https://a.at/karte.pdf", "pdf", "Club Mate"),
        ]

    def test_passes_whole_word_option(self):
        documents = [Document("u", "html", "Tomatensuppe")]
        assert search(documents, ["mate"])
        assert search(documents, ["mate"], whole_word=True) == []

    def test_no_documents(self):
        assert search([], ["Tafelspitz"]) == []
