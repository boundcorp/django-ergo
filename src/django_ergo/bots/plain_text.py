"""Catch run-together text in what a bot writes for another agent.

Some models drop the spaces between words when they write for another agent
rather than for a person: "Measured openingboundaryconfirmed: deliveredVO003guestline
ends14.352172s". The receiving bot or coding agent then has to decode it, and the
user reads it in the thread's history. Text a bot writes for the user stays
normal, so the fix is to refuse the call and ask for plain sentences once.

The check counts long tokens that aren't paths, URLs, identifiers, hashes, file
names or hyphenated compounds. Normal prose almost never has three of them, or
three in a hundred words; run-together text has dozens.
"""

from __future__ import annotations

import re

MIN_LETTERS = 17
MIN_COUNT = 3
MIN_SHARE = 0.03
MAX_ID_SWITCHES = 4  # letter/digit switches past this make a random id
_STRIP = "\"'\u201c\u201d\u2018\u2019()[]{}<>,.;:!?*"
_NOT_PROSE = re.compile(
    r"[/\\_@=`#<>{}|\-\u2013\u2014]|[0-9a-fA-F]{10}|[A-Za-z]\.[A-Za-z]"
)
_LETTER_DIGIT = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


def run_together_words(text: str) -> list[str]:
    """Long tokens that look like words with the spaces taken out."""
    found = []
    for raw in text.split():
        word = raw.strip(_STRIP)
        if len(word) < MIN_LETTERS or _NOT_PROSE.search(word):
            continue
        if len(_LETTER_DIGIT.findall(word)) > MAX_ID_SWITCHES:  # N2lVS1w4EtoT3dr4eOWO
            continue
        found.append(word)
    return found


def check_spacing(text: str, what: str) -> None:
    """Raise ValueError when ``text`` reads as words run together."""
    found = run_together_words(text)
    if len(found) < MIN_COUNT or len(found) < MIN_SHARE * len(text.split()):
        return
    examples = ", ".join(f'"{w}"' for w in found[:3])
    msg = (
        f"Not sent: the {what} has words run together ({examples}). The agent "
        "that reads it, and the user who reads the thread, need plain sentences "
        "with normal spaces, as you would write to a person. Rewrite it and call "
        "again."
    )
    raise ValueError(msg)
