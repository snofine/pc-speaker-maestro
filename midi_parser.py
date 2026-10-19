"""
Парсер стандартных MIDI-файлов с приведением полифонии к монофоническому потоку.

Используется библиотека mido. Алгоритмы сплющивания:
  - «highest» — в каждый момент времени звучит самая высокая активная нота;
  - «leading_track» — только дорожка с наибольшим числом нот (без ударных).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union

try:
    import mido
except ImportError as exc:
    raise ImportError(
        "Для работы с MIDI установите mido: pip install mido"
    ) from exc

from manual_sequencer import SequenceEvent
from speaker_controller import midi_note_to_frequency

# Ударная дорожка в General MIDI — канал 10 (индекс 9).
_DRUM_CHANNEL = 9


class FlattenMode(str, Enum):
    """Режим сведения многоголосия к одной линии."""

    HIGHEST = "highest"
    LEADING_TRACK = "leading_track"


@dataclass
class _TimedNote:
    """Нота с абсолютным временем начала и конца в секундах."""

    start: float
    end: float
    pitch: int


class MidiParser:
    """Загрузка MIDI и конвертация в последовательность для PC Speaker."""

    def __init__(self, flatten_mode: FlattenMode = FlattenMode.HIGHEST) -> None:
        self.flatten_mode = flatten_mode
        self._events: List[SequenceEvent] = []
        self._source_tempo_bpm: float = 120.0

    @property
    def events(self) -> List[SequenceEvent]:
        return list(self._events)

    @property
    def source_tempo_bpm(self) -> float:
        """Темп исходного файла (последний meta set_tempo), по умолчанию 120 BPM."""
        return self._source_tempo_bpm

    def load(self, path: Union[str, Path]) -> List[SequenceEvent]:
        """Читает MIDI-файл и строит монофоническую последовательность."""
        midi_path = Path(path)
        if not midi_path.is_file():
            raise FileNotFoundError(f"MIDI-файл не найден: {midi_path}")

        mid = mido.MidiFile(str(midi_path))
        notes = _extract_notes(mid)

        if self.flatten_mode == FlattenMode.LEADING_TRACK:
            monophonic = _flatten_leading_track(notes)
        else:
            monophonic = _flatten_highest_note(notes)

        self._events = _notes_to_sequence_events(monophonic)
        self._source_tempo_bpm = _read_file_tempo_bpm(mid)
        return self.events


def _extract_notes(mid: "mido.MidiFile") -> Dict[int, List[_TimedNote]]:
    """
    Собирает ноты по дорожкам с переводом тиков в секунды.

    Учитываются meta set_tempo для корректного тайминга.
    """
    notes_by_track: Dict[int, List[_TimedNote]] = {}
    tempo_us = 500_000  # 120 BPM по умолчанию

    for track_index, track in enumerate(mid.tracks):
        elapsed_ticks = 0
        active: Dict[Tuple[int, int], float] = {}  # (channel, pitch) -> start_sec

        for msg in track:
            elapsed_ticks += msg.time

            if msg.type == "set_tempo":
                tempo_us = msg.tempo

            if not hasattr(msg, "channel"):
                continue

            # Пропускаем ударные при выделении ведущей дорожки.
            if msg.channel == _DRUM_CHANNEL:
                continue

            time_sec = mido.tick2second(elapsed_ticks, mid.ticks_per_beat, tempo_us)

            if msg.type == "note_on" and msg.velocity > 0:
                active[(msg.channel, msg.note)] = time_sec

            elif msg.type == "note_off" or (
                msg.type == "note_on" and msg.velocity == 0
            ):
                key = (msg.channel, msg.note)
                if key in active:
                    start = active.pop(key)
                    note = _TimedNote(start=start, end=time_sec, pitch=msg.note)
                    notes_by_track.setdefault(track_index, []).append(note)

    return notes_by_track


def _flatten_leading_track(
    notes_by_track: Dict[int, List[_TimedNote]],
) -> List[Tuple[float, float, int]]:
    """
    Выбирает «ведущую» дорожку — с максимальным числом нот —
    и возвращает только её ноты как (start, end, pitch).
    """
    if not notes_by_track:
        return []

    leading_index = max(notes_by_track, key=lambda t: len(notes_by_track[t]))
    leading = sorted(notes_by_track[leading_index], key=lambda n: n.start)

    return [(n.start, n.end, n.pitch) for n in leading]


def _flatten_highest_note(
    notes_by_track: Dict[int, List[_TimedNote]],
) -> List[Tuple[float, float, int]]:
    """
    Алгоритм «самая высокая нота в каждый момент».

    1. Строим список граничных событий: начало и конец каждой ноты.
    2. Между соседними границами времени активный набор нот постоянен.
    3. Для каждого интервала берём max(pitch) среди активных нот.
    4. Склеиваем соседние интервалы с одинаковой высотой.
    """
    boundaries: List[Tuple[float, int, int, bool]] = []  # time, pitch, track, is_start

    for track_idx, notes in notes_by_track.items():
        for note in notes:
            boundaries.append((note.start, note.pitch, track_idx, True))
            boundaries.append((note.end, note.pitch, track_idx, False))

    if not boundaries:
        return []

    boundaries.sort(key=lambda x: (x[0], not x[3]))  # при совпадении времени сначала note_off

    # active: (track_idx, pitch) — уникальные звучащие ноты
    active: Dict[Tuple[int, int], int] = {}
    result: List[Tuple[float, float, int]] = []

    prev_time = boundaries[0][0]
    prev_pitch: Optional[int] = None

    for time_sec, pitch, track_idx, is_start in boundaries:
        if time_sec > prev_time and prev_pitch is not None:
            result.append((prev_time, time_sec, prev_pitch))

        key = (track_idx, pitch)
        if is_start:
            active[key] = pitch
        else:
            active.pop(key, None)

        prev_time = time_sec
        prev_pitch = max(active.values()) if active else None

    return _merge_adjacent_segments(result)


def _merge_adjacent_segments(
    segments: List[Tuple[float, float, int]],
) -> List[Tuple[float, float, int]]:
    """Объединяет соседние отрезки с одинаковым pitch без зазора."""
    if not segments:
        return []

    merged: List[Tuple[float, float, int]] = []
    cur_start, cur_end, cur_pitch = segments[0]

    for start, end, pitch in segments[1:]:
        if pitch == cur_pitch and abs(start - cur_end) < 1e-9:
            cur_end = end
        else:
            merged.append((cur_start, cur_end, cur_pitch))
            cur_start, cur_end, cur_pitch = start, end, pitch

    merged.append((cur_start, cur_end, cur_pitch))
    return merged


def _notes_to_sequence_events(
    segments: List[Tuple[float, float, int]],
) -> List[SequenceEvent]:
    """
    Конвертирует (start_sec, end_sec, midi_pitch) в SequenceEvent с паузами.

    Между нотами вставляются паузы (frequency_hz=None), если есть разрыв.
    """
    if not segments:
        return []

    segments = sorted(segments, key=lambda s: s[0])
    events: List[SequenceEvent] = []
    cursor = segments[0][0]

    for start, end, pitch in segments:
        if end <= start:
            continue

        gap_ms = int(round((start - cursor) * 1000))
        if gap_ms > 0:
            events.append(SequenceEvent(frequency_hz=None, duration_ms=gap_ms))

        duration_ms = max(1, int(round((end - start) * 1000)))
        freq = midi_note_to_frequency(pitch)
        events.append(SequenceEvent(frequency_hz=freq, duration_ms=duration_ms))
        cursor = end

    return events


def _read_file_tempo_bpm(mid: "mido.MidiFile") -> float:
    """Читает первый meta set_tempo в файле; иначе 120 BPM."""
    tempo_us = 500_000
    for track in mid.tracks:
        for msg in track:
            if msg.type == "set_tempo":
                tempo_us = msg.tempo
                return 60_000_000 / tempo_us
    return 120.0


def load_midi_file(
    path: Union[str, Path],
    flatten_mode: FlattenMode = FlattenMode.HIGHEST,
) -> Tuple[List[SequenceEvent], float]:
    """Загрузить MIDI и вернуть (события, исходный BPM файла)."""
    parser = MidiParser(flatten_mode=flatten_mode)
    events = parser.load(path)
    return events, parser.source_tempo_bpm
