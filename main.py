#!/usr/bin/env python3
"""
PC Speaker Maestro — консольное приложение для воспроизведения звука
через встроенный системный динамик материнской платы.

Требуется Python 3.10+.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional

from manual_sequencer import ManualSequencer, SequenceEvent
from midi_parser import FlattenMode, MidiParser, load_midi_file
from speaker_controller import SpeakerController

# Темп по умолчанию (ударов в минуту); влияет на масштаб длительностей при воспроизведении.
DEFAULT_BPM = 120


class MaestroApp:
    """Главный управляющий модуль с текстовым меню."""

    def __init__(self) -> None:
        self.speaker = SpeakerController()
        self.sequencer = ManualSequencer()
        self.midi_parser = MidiParser()
        self.playback_bpm: float = DEFAULT_BPM
        self.reference_bpm: float = DEFAULT_BPM  # BPM, относительно которого заданы длительности
        self._current_events: List[SequenceEvent] = []
        self._running = True

    def run(self) -> None:
        """Главный цикл приложения."""
        print("=" * 50)
        print("  PC Speaker Maestro")
        print("  Встроенный динамик материнской платы (PIT / порт 0x61)")
        print("=" * 50)

        try:
            self.speaker.initialize()
        except OSError as exc:
            print(f"Ошибка инициализации динамика: {exc}", file=sys.stderr)
            return

        while self._running:
            self._print_menu()
            choice = input("\nВыберите пункт меню: ").strip()
            self._handle_choice(choice)

        self._cleanup()

    def _print_menu(self) -> None:
        print("\n--- Меню ---")
        print("1. Загрузить MIDI-файл")
        print("2. Ввести текстовую последовательность нот")
        print("3. Загрузить последовательность из файла")
        print("4. Воспроизвести текущую мелодию")
        print("5. Изменить темп воспроизведения (BPM)")
        print(f"   [текущий темп: {self.playback_bpm:.1f} BPM]")
        print("6. Выход")

    def _handle_choice(self, choice: str) -> None:
        actions = {
            "1": self._load_midi,
            "2": self._input_manual_sequence,
            "3": self._load_sequence_file,
            "4": self._play_current,
            "5": self._change_bpm,
            "6": self._exit,
        }
        action = actions.get(choice)
        if action:
            action()
        else:
            print("Неверный пункт меню. Введите число от 1 до 6.")

    def _load_midi(self) -> None:
        path_str = input("Путь к MIDI-файлу: ").strip().strip('"')
        if not path_str:
            print("Путь не указан.")
            return

        mode_str = input(
            "Режим сведения [highest/leading_track] (по умолчанию highest): "
        ).strip().lower()
        mode = FlattenMode.LEADING_TRACK if mode_str == "leading_track" else FlattenMode.HIGHEST

        try:
            events, source_bpm = load_midi_file(path_str, flatten_mode=mode)
            self._current_events = events
            self.reference_bpm = source_bpm
            print(
                f"Загружено событий: {len(events)}, "
                f"исходный темп: {source_bpm:.1f} BPM, режим: {mode.value}"
            )
        except Exception as exc:
            print(f"Ошибка загрузки MIDI: {exc}")

    def _input_manual_sequence(self) -> None:
        print(
            "Введите последовательность (пары: нота/частота/пауза и длительность в мс).\n"
            "Пример: C4 400 D4 400 E4 400 G4 800 P 200\n"
            "Пустая строка — отмена."
        )
        line = input("> ").strip()
        if not line:
            return
        try:
            self._current_events = self.sequencer.load_from_string(line)
            self.reference_bpm = DEFAULT_BPM
            print(f"Разобрано событий: {len(self._current_events)}")
        except ValueError as exc:
            print(f"Ошибка разбора: {exc}")

    def _load_sequence_file(self) -> None:
        path_str = input("Путь к текстовому файлу с нотами: ").strip().strip('"')
        if not path_str:
            return
        try:
            self._current_events = self.sequencer.load_from_file(path_str)
            self.reference_bpm = DEFAULT_BPM
            print(f"Загружено событий: {len(self._current_events)}")
        except (OSError, ValueError) as exc:
            print(f"Ошибка: {exc}")

    def _play_current(self) -> None:
        if not self._current_events:
            print("Нет загруженной мелодии. Сначала загрузите MIDI или введите ноты.")
            return

        scale = self.reference_bpm / self.playback_bpm
        print(f"Воспроизведение ({len(self._current_events)} событий, темп {self.playback_bpm:.1f} BPM)...")

        try:
            for event in self._current_events:
                duration = max(1, int(round(event.duration_ms * scale)))
                if event.frequency_hz is None:
                    self.speaker.silence(duration)
                else:
                    self.speaker.beep(event.frequency_hz, duration)
        except KeyboardInterrupt:
            print("\nВоспроизведение прервано.")
        except OSError as exc:
            print(f"Ошибка воспроизведения: {exc}")

    def _change_bpm(self) -> None:
        raw = input(f"Новый темп BPM (сейчас {self.playback_bpm:.1f}): ").strip()
        if not raw:
            return
        try:
            bpm = float(raw.replace(",", "."))
            if bpm <= 0:
                raise ValueError("BPM должен быть положительным")
            self.playback_bpm = bpm
            print(f"Темп установлен: {self.playback_bpm:.1f} BPM")
        except ValueError as exc:
            print(f"Некорректное значение: {exc}")

    def _exit(self) -> None:
        print("Завершение работы...")
        self._running = False

    def _cleanup(self) -> None:
        self.speaker.shutdown()
        self._current_events.clear()
        print("Ресурсы освобождены. До свидания!")


def main() -> None:
    if sys.version_info < (3, 10):
        print("Требуется Python 3.10 или выше.", file=sys.stderr)
        sys.exit(1)

    app = MaestroApp()
    app.run()


if __name__ == "__main__":
    main()
