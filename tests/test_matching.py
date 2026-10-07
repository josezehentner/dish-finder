import pytest

from dish_finder.config import SNIPPET_RADIUS
from dish_finder.matching import Document, Hit, find_hits, normalize, search


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
                before="Heute: ",
                match="KASPRESSKNOEDEL",
                after=" in Rindsuppe",
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
        text = "a " * 250 + "Tafelspitz" + " b" * 250
        hit = find_hits(text, ["Tafelspitz"], "u", "html")[0]

        assert hit.match == "Tafelspitz"
        assert len(hit.snippet) <= len("Tafelspitz") + 2 * (SNIPPET_RADIUS + 1)


class TestSnippets:
    """Snippets show the document's own spelling, not the normalized search text."""

    def hit(self, text: str, term: str) -> Hit:
        [hit] = find_hits(text, [term], "u", "pdf")
        return hit

    def test_keeps_original_case_and_umlauts(self):
        hit = self.hit("Wiener KÄSESPÄTZLE mit Röstzwiebeln", "kaesespaetzle")
        assert (hit.before, hit.match, hit.after) == ("Wiener ", "KÄSESPÄTZLE", " mit Röstzwiebeln")

    def test_spelled_out_umlauts_in_document(self):
        assert self.hit("Kaesespaetzle 12,90", "Käsespätzle").match == "Kaesespaetzle"

    def test_sharp_s_maps_back_to_one_character(self):
        hit = self.hit("1 Weißbier 0,5l", "weissbier")
        assert (hit.before, hit.match, hit.after) == ("1 ", "Weißbier", " 0,5l")

    def test_accents(self):
        assert self.hit("Café Crème 3,50", "cafe creme").match == "Café Crème"

    def test_decomposed_characters_are_shown_composed(self):
        hit = self.hit("Spa\u0308tzle", "spätzle")  # "a" + combining diaeresis
        assert hit.match == "Spätzle"

    def test_match_inside_a_longer_word(self):
        hit = self.hit("Kaspressknödelsuppe 6,90", "Kaspressknödel")
        assert (hit.before, hit.match, hit.after) == ("", "Kaspressknödel", "suppe 6,90")

    def test_line_breaks_and_tabs_become_single_spaces(self):
        hit = self.hit("Suppen\n\n\tKaspressknödel   in\nRindsuppe", "Kaspressknödel")
        assert hit.snippet == "Suppen Kaspressknödel in Rindsuppe"

    def test_line_break_inside_matched_term(self):
        assert self.hit("Club\nMate 0,33l", "club mate").match == "Club Mate"

    def test_short_text_is_not_cut(self):
        hit = self.hit("Tafelspitz mit Kren", "Tafelspitz")
        assert "…" not in hit.snippet

    def test_long_text_is_cut_at_word_boundaries(self):
        words = " ".join(f"wort{i}" for i in range(100))
        hit = self.hit(f"{words} Tafelspitz {words}", "Tafelspitz")

        assert hit.before.startswith("…wort")
        assert hit.after.endswith("…")
        # no half words next to the ellipses: every word is complete
        complete_words = {*words.split(), "Tafelspitz"}
        assert all(word.strip("…") in complete_words for word in hit.snippet.split())

    def test_snippet_property_joins_the_parts(self):
        hit = Hit("t", "u", "html", 1, "…vor ", "Mitte", " nach…")
        assert hit.snippet == "…vor Mitte nach…"


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
