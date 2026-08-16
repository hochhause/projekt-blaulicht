"""Translate .NET-flavoured regular expressions to Python's :mod:`re` dialect.

The salvaged requirement patterns were authored for the original bot's .NET
runtime, so their **named groups** use .NET syntax that Python does not accept:

    .NET                     Python
    ----                     ------
    (?'name'...)             (?P<name>...)
    (?<name>...)             (?P<name>...)     # but NOT lookbehind (?<= / (?<!
    \k'name'                 (?P=name)         # backreference
    \k<name>                 (?P=name)

Everything else the asset files actually use (``\d``, ``\w``, non-capturing
``(?:...)``, alternation, character classes) is identical in both engines, so
this module deliberately handles *only* the named-group differences. Keeping
the translation minimal makes it easy to audit and hard to break.
"""

from __future__ import annotations

import re

# (?'name'  →  (?P<name>
_NET_QUOTED_GROUP = re.compile(r"\(\?'([A-Za-z_]\w*)'")
# (?<name>  →  (?P<name>   — a name char after '?<' means a group, not a
# lookbehind (which is always (?<= or (?<!).
_NET_ANGLE_GROUP = re.compile(r"\(\?<([A-Za-z_]\w*)>")
# \k'name' or \k<name>  →  (?P=name)
_NET_BACKREF = re.compile(r"\\k[<']([A-Za-z_]\w*)[>']")


def to_python(pattern: str) -> str:
    """Return *pattern* with .NET named-group syntax rewritten for Python."""
    pattern = _NET_QUOTED_GROUP.sub(r"(?P<\1>", pattern)
    pattern = _NET_ANGLE_GROUP.sub(r"(?P<\1>", pattern)
    pattern = _NET_BACKREF.sub(r"(?P=\1)", pattern)
    return pattern


def compile(pattern: str, flags: int = re.IGNORECASE) -> re.Pattern[str]:
    """Compile a .NET-flavoured *pattern* into a Python regex.

    Defaults to case-insensitive matching: requirement text and the patterns
    do not always agree on capitalisation ("RTW" vs "rtw", sentence case).
    """
    return re.compile(to_python(pattern), flags)
