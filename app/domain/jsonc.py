"""Lenient JSON ("JSON-with-commas") loader.

The salvaged Leitstellenspiel vehicle-asset files (``*.mscv``) are *almost*
strict JSON, but 25 of the 186 files contain trailing commas before a closing
``}`` or ``]`` — a habit from the original (C#/JSON5-tolerant) tooling that
Python's :func:`json.loads` rejects.

Rather than pull in a JSON5 dependency for one small quirk, this module strips
structurally-trailing commas in a **string-aware** pass, then hands the result
to the stdlib parser. String-awareness matters: the asset files are full of
regular expressions whose contents may legitimately contain commas and braces
(e.g. ``"(\\d{1,2}) Polizisten"``), and a naive ``re.sub`` would corrupt them.

The only tolerated deviation from strict JSON is the trailing comma. Anything
else (comments, single quotes, unquoted keys) still raises, on purpose — we
want to notice if the upstream data format ever drifts further.
"""

from __future__ import annotations

import json
from typing import Any


def strip_trailing_commas(raw: str) -> str:
    """Return *raw* with structurally-trailing commas removed.

    A comma is "structurally trailing" when it sits outside any string literal
    and the next non-whitespace character is ``}`` or ``]``. Commas inside
    string literals (including escaped quotes) are left untouched.
    """
    out: list[str] = []
    in_string = False
    escaped = False
    i, n = 0, len(raw)

    while i < n:
        ch = raw[i]

        if in_string:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            i += 1
            continue

        if ch == '"':
            in_string = True
            out.append(ch)
        elif ch == ",":
            # Look ahead past whitespace: if a closer follows, this comma is
            # trailing and gets dropped; otherwise it is a real separator.
            j = i + 1
            while j < n and raw[j] in " \t\r\n":
                j += 1
            if j < n and raw[j] in "}]":
                pass  # drop the trailing comma
            else:
                out.append(ch)
        else:
            out.append(ch)
        i += 1

    return "".join(out)


def loads(raw: str) -> Any:
    """Parse a lenient-JSON string (trailing commas allowed)."""
    return json.loads(strip_trailing_commas(raw))


def load_file(path: str, *, encoding: str = "utf-8") -> Any:
    """Read and parse a lenient-JSON file.

    Always decoded as UTF-8 — the asset files carry German umlauts as UTF-8
    (e.g. ``ö`` = ``0xC3 0xB6``), and reading them any other way would corrupt
    the regexes that must match live German mission text.
    """
    with open(path, encoding=encoding) as fh:
        return loads(fh.read())
