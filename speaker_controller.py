"""
Модуль прямого управления системным динамиком (PC Speaker) материнской платы.

Физический динамик подключён к чипу PIT (канал 2) и порту 0x61.
Управление идёт напрямую через I/O-порты, а не через звуковую карту.

  Windows: inpout32.dll / inpoutx64.dll (драйвер InpOut, нужны права администратора)
  Linux:   ioctl KIOCSOUND на /dev/console или /dev/tty5, либо утилита beep (pcspkr)
"""

from __future__ import annotations

import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

# Программные пределы частоты согласно ТЗ и делителю PIT (16 бит).
MIN_FREQUENCY_HZ = 37
MAX_FREQUENCY_HZ = 32767

# Аппаратные порты IBM PC для встроенного динамика.
_TIMER_CMD_PORT = 0x43
_TIMER_DATA_PORT = 0x42
_PC_SPEAKER_PORT = 0x61
_PIT_BASE_FREQ = 1_193_180  # тактовая частота таймера 8253/8254


def clamp_frequency(frequency: float) -> int:
    """Ограничивает частоту допустимым диапазоном и возвращает целое значение в Гц."""
    return int(max(MIN_FREQUENCY_HZ, min(MAX_FREQUENCY_HZ, round(frequency))))


class _WindowsInpOutDriver:
    """
    Доступ к портам ввода-вывода через библиотеку InpOut32/64.

    Без этой DLL (и драйвера) Windows не даёт писать в 0x42/0x61 из user-mode,
    поэтому winsound.Beep здесь намеренно не используется — он часто играет
    через звуковую карту, а не через пищалку на материнке.
    """

    def __init__(self) -> None:
        self._dll = None
        self._out32: Optional[Callable[[int, int], None]] = None
        self._inp32: Optional[Callable[[int], int]] = None
        self._loaded_path: Optional[Path] = None

    @property
    def is_ready(self) -> bool:
        return self._out32 is not None and self._inp32 is not None

    @property
    def dll_path(self) -> Optional[Path]:
        return self._loaded_path

    def load(self) -> None:
        """Ищет и загружает inpoutx64.dll или inpout32.dll под разрядность Python."""
        if self.is_ready:
            return

        import ctypes
        import struct

        # На 64-битном Python нужна только inpoutx64.dll (32-битная даст WinError 193).
        is_64bit_python = struct.calcsize("P") == 8
        dll_names = ("inpoutx64.dll",) if is_64bit_python else ("inpout32.dll",)

        driver_closed_error: Optional[OSError] = None
        not_found = True

        for directory in _inpout_search_directories():
            for name in dll_names:
                path = directory / name
                if not path.is_file():
                    continue
                not_found = False
                try:
                    dll = ctypes.WinDLL(str(path))
                    out32 = dll.Out32
                    out32.argtypes = [ctypes.c_short, ctypes.c_short]
                    out32.restype = None

                    inp32 = dll.Inp32
                    inp32.argtypes = [ctypes.c_short]
                    inp32.restype = ctypes.c_short

                    is_open = getattr(dll, "IsInpOutDriverOpen", None)
                    if is_open is not None:
                        is_open.restype = ctypes.c_int
                        if not is_open():
                            driver_closed_error = OSError(
                                f"Драйвер InpOut не открыл порты ({path.name}). "
                                "Запустите программу от имени администратора."
                            )
                            continue

                    self._dll = dll
                    self._out32 = out32
                    self._inp32 = inp32
                    self._loaded_path = path
                    return
                except OSError:
                    continue

        hint = (
            "Положите inpoutx64.dll (для 64-bit Python) или inpout32.dll "
            f"(для 32-bit Python) в папку pc_speaker или в {Path(__file__).resolve().parent.parent}."
        )
        if driver_closed_error:
            raise OSError(f"{driver_closed_error} {hint}") from driver_closed_error
        if not_found:
            raise OSError(f"Не найдены inpoutx64.dll / inpout32.dll. {hint}")
        raise OSError(f"Не удалось загрузить InpOut. {hint}")

    def play_tone(self, frequency_hz: int, duration_ms: int) -> None:
        """Включает тон на PIT и гейт динамика, затем выключает."""
        if not self._out32 or not self._inp32:
            raise OSError("InpOut не инициализирован")

        divisor = _PIT_BASE_FREQ // frequency_hz
        if divisor <= 0 or divisor > 0xFFFF:
            self.silence()
            return

        # Режим 3 (квадрат), канал 2, бинарный счёт — команда 0xB6.
        self._out32(_TIMER_CMD_PORT, 0xB6)
        self._out32(_TIMER_DATA_PORT, divisor & 0xFF)
        self._out32(_TIMER_DATA_PORT, (divisor >> 8) & 0xFF)

        # Биты 0 и 1 порта 0x61 включают прохождение сигнала на динамик.
        speaker_state = self._inp32(_PC_SPEAKER_PORT) & 0xFFFF
        self._out32(_PC_SPEAKER_PORT, speaker_state | 0x03)

        time.sleep(duration_ms / 1000.0)

        self._out32(_PC_SPEAKER_PORT, speaker_state & 0xFC)

    def silence(self) -> None:
        """Гасит динамик, не трогая остальные биты порта 0x61."""
        if not self._out32 or not self._inp32:
            return
        speaker_state = self._inp32(_PC_SPEAKER_PORT) & 0xFFFF
        self._out32(_PC_SPEAKER_PORT, speaker_state & 0xFC)

    def shutdown(self) -> None:
        self.silence()
        self._dll = None
        self._out32 = None
        self._inp32 = None


def _inpout_search_directories() -> List[Path]:
    """Каталоги, где ищем DLL InpOut (рядом со скриптом и в корне репозитория)."""
    here = Path(__file__).resolve().parent
    return [here, here.parent, Path.cwd()]


class SpeakerController:
    """
    Контроллер встроенного PC Speaker.

    Windows: только прямой доступ к портам через InpOut.
    Linux:   KIOCSOUND (/dev/console, /dev/tty5) или утилита beep.
    """

    def __init__(self) -> None:
        self._system = platform.system()
        self._linux_backend: Optional[str] = None  # "console" | "tty5" | "beep" | None
        self._windows = _WindowsInpOutDriver()
        self._initialized = False

    def initialize(self) -> None:
        """Проверяет доступность аппаратного динамика при старте."""
        if self._initialized:
            return

        if self._system == "Windows":
            self._windows.load()
            print(
                f"PC Speaker: прямой I/O через {self._windows.dll_path.name}",
                file=sys.stderr,
            )
            self._initialized = True
            return

        if self._system == "Linux":
            self._probe_linux_backend()
            if self._linux_backend:
                print(
                    f"PC Speaker: бэкенд Linux — {self._linux_backend}",
                    file=sys.stderr,
                )
            self._initialized = True
            return

        raise OSError(
            f"ОС «{self._system}» не поддерживается. "
            "Доступны только Windows и Linux."
        )

    def _probe_linux_backend(self) -> None:
        """Проверяет ioctl и beep; выбирает первый рабочий вариант."""
        for backend in ("console", "tty5", "beep"):
            if backend == "beep":
                ok = self._try_beep(37, 1)
            elif backend == "console":
                ok = self._try_kiocsound("/dev/console", 37, 1)
            else:
                ok = self._try_kiocsound("/dev/tty5", 37, 1)

            if ok:
                self._linux_backend = backend
                return

        print(
            "Предупреждение: PC Speaker недоступен. "
            "Загрузите модуль ядра pcspkr (modprobe pcspkr), "
            "запустите от root или установите утилиту beep.",
            file=sys.stderr,
        )
        self._linux_backend = None

    def beep(self, frequency_hz: float, duration_ms: int) -> None:
        """
        Воспроизводит тон на встроенном динамике материнской платы.

        :param frequency_hz: частота в герцах (37–32767)
        :param duration_ms: длительность в миллисекундах
        """
        if not self._initialized:
            self.initialize()

        if duration_ms <= 0:
            return

        freq = clamp_frequency(frequency_hz)

        if self._system == "Windows":
            self._windows.play_tone(freq, duration_ms)
        elif self._system == "Linux":
            self._beep_linux(freq, duration_ms)
        else:
            raise OSError(f"Неподдерживаемая ОС: {self._system}")

    def silence(self, duration_ms: int) -> None:
        """Пауза: динамик выключен, только ожидание."""
        if self._system == "Windows" and self._windows.is_ready:
            self._windows.silence()
        elif self._system == "Linux":
            self._linux_turn_off()

        if duration_ms > 0:
            time.sleep(duration_ms / 1000.0)

    def _beep_linux(self, frequency_hz: int, duration_ms: int) -> None:
        if self._linux_backend == "beep":
            if self._try_beep(frequency_hz, duration_ms):
                return
        elif self._linux_backend == "console":
            if self._try_kiocsound("/dev/console", frequency_hz, duration_ms):
                return
        elif self._linux_backend == "tty5":
            if self._try_kiocsound("/dev/tty5", frequency_hz, duration_ms):
                return

        self._probe_linux_backend()
        if self._linux_backend == "beep" and self._try_beep(frequency_hz, duration_ms):
            return
        if self._linux_backend == "console" and self._try_kiocsound(
            "/dev/console", frequency_hz, duration_ms
        ):
            return
        if self._linux_backend == "tty5" and self._try_kiocsound(
            "/dev/tty5", frequency_hz, duration_ms
        ):
            return

        time.sleep(duration_ms / 1000.0)

    @staticmethod
    def _try_beep(frequency_hz: int, duration_ms: int) -> bool:
        """Утилита beep — обёртка над тем же аппаратным pcspkr."""
        try:
            subprocess.run(
                ["beep", "-f", str(frequency_hz), "-l", str(duration_ms)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(duration_ms / 1000.0 + 2.0, 2.0),
            )
            return True
        except (FileNotFoundError, subprocess.CalledProcessError, PermissionError, OSError):
            return False
        except subprocess.TimeoutExpired:
            return False

    @staticmethod
    def _try_kiocsound(device: str, frequency_hz: int, duration_ms: int) -> bool:
        """
        ioctl KIOCSOUND — прямое управление частотой встроенного динамика в ядре.

        Аргумент ioctl — делитель PIT: 1193180 / Hz (как на Windows).
        """
        try:
            import fcntl

            KIOCSOUND = 0x4B2F
            divisor = int(_PIT_BASE_FREQ / frequency_hz)

            with open(device, "wb") as console:
                fcntl.ioctl(console.fileno(), KIOCSOUND, divisor)
                time.sleep(duration_ms / 1000.0)
                fcntl.ioctl(console.fileno(), KIOCSOUND, 0)

            return True
        except (FileNotFoundError, PermissionError, OSError, ImportError):
            return False

    def _linux_turn_off(self) -> None:
        if self._linux_backend in ("console", "tty5"):
            device = "/dev/console" if self._linux_backend == "console" else "/dev/tty5"
            try:
                import fcntl

                with open(device, "wb") as console:
                    fcntl.ioctl(console.fileno(), 0x4B2F, 0)
            except OSError:
                pass

    def shutdown(self) -> None:
        """Выключает тон и освобождает драйвер."""
        if self._system == "Windows":
            self._windows.shutdown()
        elif self._system == "Linux":
            self._linux_turn_off()
        self._initialized = False


def midi_note_to_frequency(midi_note: int) -> float:
    """
    f = 440 * 2^((d - 69) / 12)

    где d — номер MIDI-ноты (0–127).
    """
    return 440.0 * (2.0 ** ((midi_note - 69) / 12.0))
