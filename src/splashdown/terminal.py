from __future__ import annotations


def terminal_text(value: str, *, multiline: bool = False) -> str:
    return "".join(
        character
        if character.isprintable() or (multiline and character in "\n\t")
        else repr(character)[1:-1]
        for character in value
    )
