import unittest

import tracker_compat


class TrackerCompatibilityTests(unittest.TestCase):
    def test_flat_rows_preserve_strings_dates_and_nulls(self):
        rows = tracker_compat.parse_application_tracker(
            """- company: Example Co
  status: 'Interview: round #1'
  applied_date: 2026-11-23
  empty_value:
  explicit_null: null
  tilde_null: ~
  quoted_null: "null"
  numeric_text: 0042
""")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "Interview: round #1")
        self.assertEqual(rows[0]["applied_date"], "2026-11-23")
        self.assertEqual(rows[0]["empty_value"], None)
        self.assertEqual(rows[0]["explicit_null"], None)
        self.assertEqual(rows[0]["tilde_null"], None)
        self.assertEqual(rows[0]["quoted_null"], "null")
        self.assertEqual(rows[0]["numeric_text"], "0042")

    def test_quoted_scalars_decode_yaml_escapes(self):
        rows = tracker_compat.parse_application_tracker(
            """- double: "line\\nquote: \\"ready\\""
  single: 'It''s "ready"'
  unicode: "\\u2603"
  escaped_hash: "a # b" # comment
  plain_hash: value#fragment
""")
        self.assertEqual(rows[0]["double"], 'line\nquote: "ready"')
        self.assertEqual(rows[0]["single"], 'It\'s "ready"')
        self.assertEqual(rows[0]["unicode"], "☃")
        self.assertEqual(rows[0]["escaped_hash"], "a # b")
        self.assertEqual(rows[0]["plain_hash"], "value#fragment")

    def test_multiline_quoted_scalars_fold_and_keep_escaped_breaks(self):
        rows = tracker_compat.parse_application_tracker(
            """- single: 'First line
    second line

    third line'
  double: "First line
    second line"
  escaped: "joined\\
    without a space"
""")
        self.assertEqual(rows[0]["single"], "First line second line\nthird line")
        self.assertEqual(rows[0]["double"], "First line second line")
        self.assertEqual(rows[0]["escaped"], "joinedwithout a space")

    def test_literal_and_folded_block_notes_are_complete(self):
        rows = tracker_compat.parse_application_tracker(
            """- company: Example Co
  notes: |-
    First line: # kept as text
    Second line: \"still text\"
  status: applied
  details: >-
    First paragraph line
    continues here

    Second paragraph
""")
        self.assertEqual(rows[0]["notes"],
                         'First line: # kept as text\nSecond line: "still text"')
        self.assertEqual(rows[0]["status"], "applied")
        self.assertEqual(rows[0]["details"],
                         "First paragraph line continues here\n\nSecond paragraph")

    def test_default_block_chomping_keeps_one_final_newline(self):
        rows = tracker_compat.parse_application_tracker(
            """- notes: |
    first line
    second line
  status: applied
""")
        self.assertEqual(rows[0]["notes"], "first line\nsecond line\n")
        self.assertEqual(rows[0]["status"], "applied")

    def test_keep_chomping_and_explicit_block_indentation(self):
        rows = tracker_compat.parse_application_tracker(
            """- notes: |+2
    complete block


  status: true
""")
        self.assertEqual(rows[0]["notes"], "complete block\n\n\n")
        self.assertEqual(rows[0]["status"], "true")

    def test_plain_multiline_note_uses_yaml_folding(self):
        rows = tracker_compat.parse_application_tracker(
            """- notes: First line
    Second line

    New paragraph
  status: 'Interview'
""")
        self.assertEqual(rows[0]["notes"], "First line Second line\nNew paragraph")
        self.assertEqual(rows[0]["status"], "Interview")

    def test_explicit_empty_list_and_document_markers(self):
        self.assertEqual(tracker_compat.parse_application_tracker("---\n[]\n...\n"), [])
        rows = tracker_compat.parse_application_tracker("---\n- status: applied\n...\n")
        self.assertEqual(rows, [{"status": "applied"}])
        with self.assertRaises(tracker_compat.TrackerCompatibilityError):
            tracker_compat.parse_application_tracker("# comment only\n")
        self.assertEqual(tracker_compat.parse_application_tracker(
            "# comment only\n", allow_comment_only=True), [])

    def test_rejects_ambiguous_or_unsupported_yaml(self):
        invalid = (
            "",
            "# no rows\n",
            "- status: applied\n  status: interviewing\n",
            "- details: {stage: interview}\n",
            "- details: [a, b]\n",
            "- details: value: ambiguous\n",
            "- details: \"bad \\q escape\"\n",
            "- status: 'applied'\n    nested: no\n",
            "- notes: |2\n   under-indented\n",
            "[]\n- status: applied\n",
            "---\n- status: applied\n---\n- status: rejected\n",
            "---\n- status: applied\n---\n- status: rejected\n",
        )
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises(tracker_compat.TrackerCompatibilityError):
                    tracker_compat.parse_application_tracker(text)


if __name__ == "__main__":
    unittest.main()
