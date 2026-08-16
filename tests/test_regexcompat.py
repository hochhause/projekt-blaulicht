import re
import unittest

from app.domain import regexcompat


class RegexCompatTests(unittest.TestCase):
    def test_dotnet_quoted_group(self):
        out = regexcompat.to_python("(?'water'\\d+) l")
        self.assertEqual(out, "(?P<water>\\d+) l")
        self.assertEqual(re.compile(out).match("42 l").group("water"), "42")

    def test_dotnet_angle_group(self):
        self.assertEqual(regexcompat.to_python("(?<crew>\\d+)"), "(?P<crew>\\d+)")

    def test_lookbehind_is_not_touched(self):
        # (?<= and (?<! are lookbehind, not named groups — must be left alone.
        self.assertEqual(regexcompat.to_python("(?<=x)y"), "(?<=x)y")
        self.assertEqual(regexcompat.to_python("(?<!x)y"), "(?<!x)y")

    def test_backreference(self):
        self.assertEqual(regexcompat.to_python("(?'n'\\d) \\k'n'"), "(?P<n>\\d) (?P=n)")

    def test_real_firetruck_pattern_compiles_and_captures(self):
        raw = "(?'firetrucksNeededWater'\\d+) l\\. Wasser"
        pat = regexcompat.compile(raw)
        m = pat.search("insgesamt 4000 l. Wasser benötigt")
        self.assertIsNotNone(m)
        self.assertEqual(m.group("firetrucksNeededWater"), "4000")

    def test_case_insensitive_by_default(self):
        self.assertIsNotNone(regexcompat.compile("RTW").search("ein rtw fehlt"))


if __name__ == "__main__":
    unittest.main()
