"""Prompt files and untrusted-text escaping, shared by every LLM node.

All user- or data-derived text goes into a prompt through `block` (multi-line) or `line`
(one list entry), so no input can close a delimiter or fake extra entries.
"""

import re
from functools import cache
from pathlib import Path

_DIR = Path(__file__).parent / "prompts"
_WS = re.compile(r"\s+")


@cache
def load_prompt(name: str) -> str:
    return (_DIR / f"{name}.md").read_text(encoding="utf-8")


def clean(text: object) -> str:
    """Escape angle brackets so untrusted text cannot open or close a delimiter."""
    return str(text).replace("<", "&lt;").replace(">", "&gt;")


def line(text: object) -> str:
    """clean() plus single-line, so a stored value cannot fake extra list entries."""
    return _WS.sub(" ", clean(text)).strip()


def block(tag: str, text: object) -> str:
    """<tag>\\n{escaped text}\\n</tag>"""
    return f"<{tag}>\n{clean(text)}\n</{tag}>"


def items(tag: str, entries: list[str]) -> str:
    """A delimited list block whose entries were already built with line(); '(none)' if empty."""
    return f"<{tag}>\n{chr(10).join(entries) or '(none)'}\n</{tag}>"
