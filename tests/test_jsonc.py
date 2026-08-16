import unittest

from app.domain import jsonc


class TrailingCommaTests(unittest.TestCase):
    def test_object_trailing_comma(self):
        self.assertEqual(jsonc.loads('{"a": 1, "b": 2,}'), {"a": 1, "b": 2})

    def test_array_trailing_comma(self):
        self.assertEqual(jsonc.loads("[1, 2, 3,]"), [1, 2, 3])

    def test_trailing_comma_with_whitespace_and_newlines(self):
        self.assertEqual(jsonc.loads('{\n  "a": 1,\n}\n'), {"a": 1})

    def test_nested(self):
        raw = '{"x": [1, 2,], "y": {"z": 3,},}'
        self.assertEqual(jsonc.loads(raw), {"x": [1, 2], "y": {"z": 3}})

    def test_comma_brace_inside_string_is_preserved(self):
        # The crux: a comma-then-brace *inside a regex string* must survive.
        raw = '{"pattern": "(\\\\d{1,2}) Einsatz,}"}'
        self.assertEqual(jsonc.loads(raw), {"pattern": "(\\d{1,2}) Einsatz,}"})

    def test_plain_valid_json_unchanged(self):
        self.assertEqual(jsonc.loads('{"a": 1}'), {"a": 1})

    def test_escaped_quote_in_string(self):
        self.assertEqual(jsonc.loads('{"a": "he said \\"hi\\"",}'), {"a": 'he said "hi"'})


if __name__ == "__main__":
    unittest.main()
