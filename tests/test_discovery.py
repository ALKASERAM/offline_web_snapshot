import unittest

from offline_snapshot.discovery import classify_candidate, new_visible_text, scan_candidates


def candidate(**changes):
    value = {
        "selector": "#control",
        "tag": "button",
        "role": "",
        "type": "button",
        "label": "Show details",
        "href": "",
        "download": False,
        "disabled": False,
        "visible": True,
        "inForm": False,
        "contentEditable": False,
        "included": True,
        "excluded": False,
        "policyText": "Show details control",
    }
    value.update(changes)
    return value


class InteractionPolicyTests(unittest.TestCase):
    url = "https://example.test/start"

    def test_safe_non_form_controls_are_eligible(self):
        self.assertIsNone(classify_candidate(candidate(), self.url))
        self.assertIsNone(
            classify_candidate(
                candidate(tag="a", role="tab", href="https://example.test/start#details"), self.url
            )
        )

    def test_forms_mutations_and_unusable_controls_are_skipped(self):
        cases = [
            (candidate(inForm=True), "form control"),
            (
                candidate(label="Delete record", policyText="Delete record danger"),
                "potential account or data mutation",
            ),
            (candidate(disabled=True), "disabled"),
            (candidate(visible=False), "not visible"),
            (candidate(label="", policyText=""), "unnamed control"),
            (candidate(excluded=True), "excluded by selector"),
            (candidate(included=False), "outside included selectors"),
        ]
        for value, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(classify_candidate(value, self.url), reason)

    def test_navigation_is_bounded_to_button_like_same_page_controls(self):
        self.assertEqual(
            classify_candidate(
                candidate(tag="a", href="https://other.test/start#details"), self.url
            ),
            "cross-origin navigation",
        )
        self.assertEqual(
            classify_candidate(candidate(tag="a", href="https://example.test/other"), self.url),
            "ordinary navigation link",
        )
        self.assertIsNone(
            classify_candidate(
                candidate(tag="a", href="https://example.test/start#details"), self.url
            )
        )
        self.assertEqual(
            classify_candidate(candidate(tag="a", href="mailto:test@example.test"), self.url),
            "non-HTTP navigation",
        )

    def test_new_visible_text_provides_a_bounded_replay_assertion(self):
        self.assertEqual(
            new_visible_text("Heading\nWaiting", "Heading\nRecorded result"), "Recorded result"
        )
        self.assertEqual(
            new_visible_text(
                "Show details Results tab", "Show details\nRecorded details\nResults tab"
            ),
            "Recorded details",
        )
        self.assertIsNone(new_visible_text("Heading", "Heading"))
        self.assertIsNone(new_visible_text("", "x" * 161))


class CandidateScanTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_user_selector_is_an_explicit_error(self):
        class Page:
            url = "https://example.test/"

            async def evaluate(self, script, options):
                return {
                    "invalid": [{"selector": "[broken", "error": "SyntaxError"}],
                    "candidates": [],
                }

        with self.assertRaisesRegex(ValueError, "Invalid interaction discovery selector"):
            await scan_candidates(Page(), include=("[broken",))


if __name__ == "__main__":
    unittest.main()
