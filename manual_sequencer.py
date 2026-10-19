"""
Текстовый секвенсор для ручного ввода мелодий.

Синтаксис (разделители — запятая или пробел):
  A4 500      — нота A четвёртой октавы, 500 мс
  C#5,250     — диез, запятая как разделитель
  440 200     — частота напрямую в герцах
  P 300       — пауза 300 мс (также допустим символ 0 вместо P)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

from speaker_controller import clamp_frequency, midi_note_to_frequency

# Имена нот в хроматической гамме (полутона от C).
_NOTE_NAMES: dict[str, int] = {
    "C": 0,
    "C#": 1,
    "DB": 1,
    "D": 2,
    "D#": 3,
    "EB": 3,
    "E": 4,
    "F": 5,
    "F#": 6,
    "GB": 6,
    "G": 7,
    "G#": 8,
    "AB": 8,
    "A": 9,
    "A#": 10,
    "BB": 10,
    "B": 11,
    "H": 11,  # си в немецкой нотации
}

# Паттерн ноты: буква + опциональный диез/бемоль + октава (0–9).
_NOTE_PATTERN = re.compile(
    r"^(?P<name>[A-Ga-gHh])(?P<accidental>[#b]?)(?P<octave>\d)$"
)

# Разбиение строки на токены по запятым и пробельным символам.
_TOKEN_SPLIT = re.compile(r"[\s,]+")


@dataclass(frozen=True)
class SequenceEvent:
    """Один элемент мелодии: звук (частота) или пауза (frequency_hz is None)."""

    frequency_hz: Optional[float]
    duration_ms: int


class ManualSequencer:
    """Парсер и хранилище текстовых последовательностей нот."""

    def __init__(self) -> None:
        self._events: List[SequenceEvent] = []

    @property
    def events(self) -> List[SequenceEvent]:
        return list(self._events)

    def clear(self) -> None:
        self._events.clear()

    def load_from_string(self, text: str) -> List[SequenceEvent]:
        """Разбирает строку с нотами и сохраняет результат."""
        self._events = parse_sequence_text(text)
        return self.events

    def load_from_file(self, path: Union[str, Path]) -> List[SequenceEvent]:
        """Читает файл и разбирает его содержимое."""
        content = Path(path).read_text(encoding="utf-8")
        return self.load_from_string(content)


def parse_sequence_text(text: str) -> List[SequenceEvent]:
    """
    Преобразует текстовую запись в список событий.

    Токены группируются парами: (нота/пауза/частота, длительность_мс).
    """
    tokens = [t for t in _TOKEN_SPLIT.split(text.strip()) if t]
    if not tokens:
        return []

    if len(tokens) % 2 != 0:
        raise ValueError(
            f"Нечётное число токенов ({len(tokens)}). "
            "Ожидаются пары: нота/частота/пауза и длительность в мс."
        )

    events: List[SequenceEvent] = []
    for i in range(0, len(tokens), 2):
        pitch_token = tokens[i]
        duration_token = tokens[i + 1]

        try:
            duration_ms = int(duration_token)
        except ValueError as exc:
            raise ValueError(
                f"Некорректная длительность «{duration_token}» "
                f"для «{pitch_token}»"
            ) from exc

        if duration_ms < 0:
            raise ValueError(f"Длительность не может быть отрицательной: {duration_ms}")

        frequency = _parse_pitch_token(pitch_token)
        events.append(SequenceEvent(frequency_hz=frequency, duration_ms=duration_ms))

    return events


def _parse_pitch_token(token: str) -> Optional[float]:
    """
    Возвращает частоту в Гц или None для паузы (P / 0).

    Поддерживает:
      - паузу: P, p, 0
      - число в Гц: 440, 261.63
      - буквенную нотацию: A4, C#5, Bb3
    """
    upper = token.strip().upper()

    if upper in ("P", "0"):
        return None

    # Прямая частота в герцах.
    if re.fullmatch(r"\d+(\.\d+)?", token):
        return float(clamp_frequency(float(token)))

    match = _NOTE_PATTERN.match(upper)
    if not match:
        raise ValueError(f"Неизвестный токен ноты или паузы: «{token}»")

    name = match.group("name").upper()
    if name == "H":
        name = "B"

    accidental = match.group("accidental")
    octave = int(match.group("octave"))

    # MIDI-номер: C4 = 60, октава в научной нотации.
    note_key = name + accidental.upper()
    if note_key not in _NOTE_NAMES:
        raise ValueError(f"Неизвестная нота: «{token}»")

    midi_note = _NOTE_NAMES[note_key] + (octave + 1) * 12
    if not 0 <= midi_note <= 127:
        raise ValueError(f"MIDI-номер вне диапазона 0–127: {midi_note} ({token})")

    return midi_note_to_frequency(midi_note)
