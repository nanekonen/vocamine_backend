import unittest
from unittest.mock import patch

from app.services import word_service


def _phrase_items(text: str, phrases: list[str]) -> list[dict]:
    with patch.object(word_service, "_phrase_catalog", return_value=phrases):
        return [
            item
            for item in word_service.analyze_lexical_items(text)
            if item["kind"] == "phrase"
        ]


class DependencyPhraseDetectionTest(unittest.TestCase):
    def test_detects_inflected_non_contiguous_verb_object_phrase(self):
        items = _phrase_items(
            "She paid close attention to the lecture.",
            ["pay attention"],
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "pay attention")
        self.assertEqual(items[0]["surface_forms"], ["paid close attention"])
        self.assertEqual(items[0]["occurrences"], [
            {"form": "paid close attention", "start": 4, "end": 24}
        ])

    def test_dependency_detection_is_primary_for_contiguous_phrase(self):
        items = _phrase_items("Please pay attention.", ["pay attention"])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "pay attention")
        self.assertEqual(items[0]["occurrence_count"], 1)

    def test_does_not_match_words_without_direct_object_relation(self):
        items = _phrase_items(
            "She paid the fee after receiving attention.",
            ["pay attention"],
        )

        self.assertEqual(items, [])

    def test_requires_catalogued_trailing_preposition_and_includes_it_in_span(self):
        matched = _phrase_items(
            "The report drew considerable attention to the problem.",
            ["draw attention to"],
        )
        unmatched = _phrase_items(
            "The report drew considerable attention away from the problem.",
            ["draw attention to"],
        )

        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["text"], "draw attention to")
        self.assertEqual(
            matched[0]["surface_forms"],
            ["drew considerable attention to"],
        )
        self.assertEqual(unmatched, [])

    def test_phrase_matcher_still_handles_non_object_phrases(self):
        items = _phrase_items("They finally give up.", ["give up"])

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "give up")


if __name__ == "__main__":
    unittest.main()
