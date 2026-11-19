"""Keep code, links, placeholders and glossary terms out of the model's hands.

Before a text is translated, every span that must come back unchanged is
replaced by a numbered token such as ``⟦3⟧``.  After the translation the tokens
are put back.  A translation that lost, duplicated or invented a token is
rejected, so a broken placeholder never reaches a file or a user.
"""
from __future__ import annotations

import re

OPEN, CLOSE = "\u27e6", "\u27e7"
_TOKEN = re.compile(OPEN + r"\s{0,3}(\d{1,4})\s{0,3}" + CLOSE)
# Order matters: the first alternative that matches at a position wins.
# Every repetition is bounded. The text may come from a web page or a file, and an open-ended pattern
# ("%" followed by 40,000 zeros, a link made of dots) took from seconds to minutes to fail.
_PROTECTED = re.compile("|".join((
    r"```.*?```",                                                    # fenced code (one unclosed fence at most)
    r"`[^`\n]{1,400}`",                                              # inline code
    r"https?://[^\s<>\"')\]]{0,2000}[^\s<>\"')\].,;:!?]",             # links, without the sentence's own punctuation
    r"(?<![\w.+-])[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,6}(?![\w-])",  # e-mail addresses
    r"\{\{.{0,300}?\}\}",                                            # {{ template }}
    r"\$\{[^}\n]{1,80}\}",                                           # ${shell}
    r"%\([A-Za-z_]\w{0,60}\)[sdif]",                                 # %(name)s
    r"\{[A-Za-z0-9_.:!,<>^ +#-]{0,40}\}",                            # {name} {0} {:.2f}
    r"%(?:\d{1,3}\$)?[-+0#]{0,4}\d{0,4}(?:\.\d{1,4})?[sdifxXeEgGcr]\b",  # %s %d %5.2f %1$s
    r"</?[A-Za-z][A-Za-z0-9-]{0,40}(?:\s[^<>]{0,200})?/?>",           # <b> </b> <br/>
    r"&[A-Za-z]{2,8};|&#\d{2,6};",                                   # &nbsp; &#160;
)), re.DOTALL)


class ProtectError(ValueError):
    """The text or the translation cannot be handled safely."""


def protect(text: str, glossary: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """``(text with tokens, replacements)``; ``replacements[n]`` is what ``⟦n⟧`` turns back into.

    Glossary terms are whole words, matched without regard to case; their
    token turns back into the *target* term.
    """
    if OPEN in text or CLOSE in text:
        raise ProtectError(f"The text contains the reserved characters {OPEN} {CLOSE}")
    keep: list[str] = []

    def stash(value: str) -> str:
        keep.append(value)
        return f"{OPEN}{len(keep) - 1}{CLOSE}"

    out = _PROTECTED.sub(lambda match: stash(match.group(0)), text)
    terms = sorted((glossary or {}).items(), key=lambda item: -len(item[0]))  # longest first: "credit note" before "credit"
    if terms:
        lookup = {source.casefold(): target for source, target in terms}
        pattern = re.compile(r"(?<![\w" + OPEN + r"])(?:" + "|".join(re.escape(source) for source, _ in terms)
                             + r")(?![\w" + CLOSE + r"])", re.IGNORECASE)
        pieces = _TOKEN.split(out)  # even indexes are text, odd indexes are the numbers of existing tokens
        for index in range(0, len(pieces), 2):
            pieces[index] = pattern.sub(lambda match: stash(lookup[match.group(0).casefold()]), pieces[index])
        out = "".join(piece if index % 2 == 0 else f"{OPEN}{piece}{CLOSE}" for index, piece in enumerate(pieces))
    if len(keep) > 400:
        raise ProtectError("Too many protected parts in one text (limit 400): translate it in pieces")
    return out, keep


def restore(translated: str, keep: list[str]) -> str:
    """Put the protected parts back. Every token must come back exactly once."""
    found = [int(number) for number in _TOKEN.findall(translated)]
    if sorted(found) != list(range(len(keep))):
        missing = sorted(set(range(len(keep))) - set(found))
        raise ProtectError(
            "The translation changed the protected parts (code, links, placeholders or glossary terms): "
            + (f"{len(missing)} missing" if missing else "some were repeated or invented"))
    out = _TOKEN.sub(lambda match: keep[int(match.group(1))], translated)
    if OPEN in out or CLOSE in out:
        raise ProtectError("The translation contains a malformed placeholder token")
    return out
