# PC Speaker Maestro

Built with codex

Консольное приложение для воспроизведения MIDI-файлов и вручную заданных мелодий через встроенный PC Speaker.

## Запуск

Нужен Python 3.10 или новее.

```powershell
python -m pip install -r requirements.txt
python main.py
```

Приложение поддерживает Windows и Linux. В Windows для прямого доступа к портам ввода-вывода требуется отдельно установить драйвер InpOut и положить подходящую DLL (`inpoutx64.dll` для 64-битного Python или `inpout32.dll` для 32-битного Python) рядом с приложением. Драйверы в репозиторий не включены.

В Linux приложение пробует `KIOCSOUND` через `/dev/console` или `/dev/tty5`, затем утилиту `beep`; может потребоваться доступ к устройству консоли.

## Исходники

`legacy/` содержит связанные ранние C++-версии MIDI-проигрывателя и теста PC Speaker.

`examples/test.mid` is a small MIDI file for trying the player.
