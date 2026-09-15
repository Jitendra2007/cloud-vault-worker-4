import re
from dataclasses import dataclass
from typing import Iterable


EPISODE_PATTERN = re.compile(r"\b(?:ep|episode|e)\s*[-._: ]*(\d+)\b", re.I)


def episode_number(value: str) -> int | None:
    match = EPISODE_PATTERN.search(value or "")
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class SequenceAudit:
    verified_through: int
    first_problem: int | None
    duplicates: tuple[int, ...]
    out_of_order: bool


def audit_episode_order(items: Iterable[tuple[int, str]]) -> SequenceAudit:
    """Audit uploaded (message_id, title-or-filename) pairs without deleting data."""
    seen: dict[int, int] = {}
    ordered = list(items)
    numbers: list[int] = []
    for message_id, label in ordered:
        number = episode_number(label)
        if number is not None:
            numbers.append(number)
            seen[number] = seen.get(number, 0) + 1
    duplicates = tuple(sorted(number for number, count in seen.items() if count > 1))
    expected = 1
    for number in sorted(seen):
        if number != expected:
            return SequenceAudit(expected - 1, expected, duplicates, numbers != sorted(numbers))
        expected += 1
    return SequenceAudit(expected - 1, None if not duplicates else expected, duplicates, numbers != sorted(numbers))
