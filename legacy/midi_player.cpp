#include <windows.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <fstream>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

typedef void(WINAPI *TYPE_OUT32)(short PortAddress, short data);
typedef short(WINAPI *TYPE_INP32)(short PortAddress);
typedef BOOL(WINAPI *TYPE_ISOPEN)(void);

static TYPE_OUT32 Out32Fn = NULL;
static TYPE_INP32 Inp32Fn = NULL;
static TYPE_ISOPEN IsInpOutDriverOpenFn = NULL;
static HMODULE InpOutDll = NULL;

static const int TIMER_CMD_PORT = 0x43;
static const int TIMER_DATA_PORT = 0x42;
static const int PC_SPEAKER_PORT = 0x61;
static const double A4_FREQ = 440.0;
static const int A4_NOTE = 69;
static const uint32_t DEFAULT_TEMPO_US = 500000; // 120 BPM

struct MidiEvent {
    uint64_t time_us;
    uint8_t note;
    bool is_on;
};

struct RawTrackEvent {
    uint64_t tick;
    uint8_t status;
    uint8_t data1;
    uint8_t data2;
    uint32_t tempo_us;
    bool is_note;
    bool is_tempo;
};

struct TempoPoint {
    uint64_t tick;
    uint32_t tempo_us;
};

struct MidiSong {
    uint16_t division;
    std::vector<MidiEvent> events;
};

static void CloseInpOut() {
    if (InpOutDll) {
        FreeLibrary(InpOutDll);
        InpOutDll = NULL;
    }
}

static bool InitInpOut() {
    InpOutDll = LoadLibraryA("inpoutx64.dll");
    if (!InpOutDll) {
        InpOutDll = LoadLibraryA("inpout32.dll");
    }
    if (!InpOutDll) {
        std::cerr << "Не найдены inpoutx64.dll / inpout32.dll рядом с программой.\n";
        return false;
    }

    Out32Fn = (TYPE_OUT32)GetProcAddress(InpOutDll, "Out32");
    Inp32Fn = (TYPE_INP32)GetProcAddress(InpOutDll, "Inp32");
    IsInpOutDriverOpenFn = (TYPE_ISOPEN)GetProcAddress(InpOutDll, "IsInpOutDriverOpen");

    if (!Out32Fn || !Inp32Fn) {
        std::cerr << "В DLL InpOut не найдены функции Out32/Inp32.\n";
        CloseInpOut();
        return false;
    }

    if (IsInpOutDriverOpenFn && !IsInpOutDriverOpenFn()) {
        std::cerr << "Драйвер InpOut не открылся. Запусти программу от администратора.\n";
        CloseInpOut();
        return false;
    }

    return true;
}

static void SilenceSpeaker() {
    if (!Inp32Fn || !Out32Fn) {
        return;
    }
    short state = Inp32Fn(PC_SPEAKER_PORT);
    Out32Fn(PC_SPEAKER_PORT, state & 0xFC);
}

static void SetSpeakerFrequency(unsigned int frequency) {
    if (!Out32Fn || !Inp32Fn || frequency < 37 || frequency > 20000) {
        SilenceSpeaker();
        return;
    }

    unsigned int divisor = 1193180u / frequency;
    if (divisor == 0 || divisor > 65535) {
        SilenceSpeaker();
        return;
    }

    Out32Fn(TIMER_CMD_PORT, 0xB6);
    Out32Fn(TIMER_DATA_PORT, divisor & 0xFF);
    Out32Fn(TIMER_DATA_PORT, (divisor >> 8) & 0xFF);

    short state = Inp32Fn(PC_SPEAKER_PORT);
    Out32Fn(PC_SPEAKER_PORT, state | 0x03);
}

static double MidiNoteToFrequency(int note) {
    return A4_FREQ * std::pow(2.0, (note - A4_NOTE) / 12.0);
}

static uint16_t ReadBE16(std::istream& in) {
    unsigned char b[2];
    in.read((char*)b, 2);
    if (!in) throw std::runtime_error("Неожиданный конец файла.");
    return (uint16_t)((b[0] << 8) | b[1]);
}

static uint32_t ReadBE32(std::istream& in) {
    unsigned char b[4];
    in.read((char*)b, 4);
    if (!in) throw std::runtime_error("Неожиданный конец файла.");
    return ((uint32_t)b[0] << 24) | ((uint32_t)b[1] << 16) | ((uint32_t)b[2] << 8) | b[3];
}

static uint32_t ReadVLQ(std::istream& in) {
    uint32_t value = 0;
    for (int i = 0; i < 4; ++i) {
        unsigned char b = 0;
        in.read((char*)&b, 1);
        if (!in) throw std::runtime_error("Неожиданный конец VLQ.");
        value = (value << 7) | (b & 0x7F);
        if ((b & 0x80) == 0) return value;
    }
    throw std::runtime_error("Слишком длинное VLQ значение.");
}

static int MidiDataBytes(uint8_t status) {
    uint8_t high = status & 0xF0;
    if (high == 0xC0 || high == 0xD0) return 1;
    if (high >= 0x80 && high <= 0xE0) return 2;
    return 0;
}

static uint64_t TicksToUs(uint64_t tick, const std::vector<TempoPoint>& tempos, uint16_t division) {
    uint64_t total_us = 0;
    uint64_t last_tick = 0;
    uint32_t tempo = DEFAULT_TEMPO_US;

    for (size_t i = 0; i < tempos.size() && tempos[i].tick <= tick; ++i) {
        if (tempos[i].tick > last_tick) {
            total_us += ((tempos[i].tick - last_tick) * tempo) / division;
            last_tick = tempos[i].tick;
        }
        tempo = tempos[i].tempo_us;
    }

    total_us += ((tick - last_tick) * tempo) / division;
    return total_us;
}

static MidiSong ReadMidi(const std::string& filename) {
    std::ifstream file(filename.c_str(), std::ios::binary);
    if (!file) {
        throw std::runtime_error("Не могу открыть MIDI файл: " + filename);
    }

    char id[4];
    file.read(id, 4);
    if (std::string(id, 4) != "MThd") {
        throw std::runtime_error("Это не Standard MIDI File: нет MThd.");
    }

    uint32_t header_len = ReadBE32(file);
    uint16_t format = ReadBE16(file);
    uint16_t tracks = ReadBE16(file);
    uint16_t division = ReadBE16(file);
    if (header_len > 6) {
        file.seekg(header_len - 6, std::ios::cur);
    }
    if (division & 0x8000) {
        throw std::runtime_error("SMPTE MIDI timing пока не поддерживается.");
    }
    if (format > 1) {
        std::cout << "Формат MIDI " << format << ": будет сыграно как общий поток событий.\n";
    }

    std::vector<RawTrackEvent> raw_events;
    std::vector<TempoPoint> tempos;
    tempos.push_back(TempoPoint{0, DEFAULT_TEMPO_US});

    for (uint16_t t = 0; t < tracks; ++t) {
        file.read(id, 4);
        if (!file) break;
        uint32_t track_len = ReadBE32(file);
        std::streamoff track_end = (std::streamoff)file.tellg() + track_len;
        if (std::string(id, 4) != "MTrk") {
            file.seekg(track_end, std::ios::beg);
            continue;
        }

        uint64_t tick = 0;
        uint8_t running_status = 0;

        while (file && (std::streamoff)file.tellg() < track_end) {
            tick += ReadVLQ(file);

            uint8_t status = 0;
            file.read((char*)&status, 1);
            if (!file) break;

            if (status < 0x80) {
                if (!running_status) {
                    throw std::runtime_error("MIDI running status без предыдущего статуса.");
                }
                file.seekg(-1, std::ios::cur);
                status = running_status;
            } else if (status < 0xF0) {
                running_status = status;
            }

            if (status == 0xFF) {
                uint8_t type = 0;
                file.read((char*)&type, 1);
                uint32_t len = ReadVLQ(file);
                if (type == 0x51 && len == 3) {
                    unsigned char b[3];
                    file.read((char*)b, 3);
                    uint32_t tempo = ((uint32_t)b[0] << 16) | ((uint32_t)b[1] << 8) | b[2];
                    tempos.push_back(TempoPoint{tick, tempo});
                } else {
                    file.seekg(len, std::ios::cur);
                }
                continue;
            }

            if (status == 0xF0 || status == 0xF7) {
                uint32_t len = ReadVLQ(file);
                file.seekg(len, std::ios::cur);
                continue;
            }

            int bytes = MidiDataBytes(status);
            if (bytes == 0) {
                continue;
            }

            uint8_t d1 = 0, d2 = 0;
            file.read((char*)&d1, 1);
            if (bytes == 2) {
                file.read((char*)&d2, 1);
            }

            uint8_t kind = status & 0xF0;
            if (kind == 0x80 || kind == 0x90) {
                raw_events.push_back(RawTrackEvent{tick, status, d1, d2, 0, true, false});
            }
        }

        file.seekg(track_end, std::ios::beg);
    }

    std::stable_sort(tempos.begin(), tempos.end(), [](const TempoPoint& a, const TempoPoint& b) {
        return a.tick < b.tick;
    });

    MidiSong song;
    song.division = division;
    for (size_t i = 0; i < raw_events.size(); ++i) {
        const RawTrackEvent& e = raw_events[i];
        bool note_on = ((e.status & 0xF0) == 0x90) && e.data2 > 0;
        bool note_off = ((e.status & 0xF0) == 0x80) || (((e.status & 0xF0) == 0x90) && e.data2 == 0);
        if (!note_on && !note_off) {
            continue;
        }
        song.events.push_back(MidiEvent{TicksToUs(e.tick, tempos, division), e.data1, note_on});
    }

    std::sort(song.events.begin(), song.events.end(), [](const MidiEvent& a, const MidiEvent& b) {
        if (a.time_us != b.time_us) return a.time_us < b.time_us;
        return a.is_on < b.is_on;
    });

    return song;
}

static void SleepUntil(uint64_t target_us, uint64_t start_tick) {
    for (;;) {
        uint64_t now_us = (GetTickCount64() - start_tick) * 1000ull;
        if (now_us >= target_us) return;
        uint64_t left_ms = (target_us - now_us) / 1000ull;
        Sleep((DWORD)std::min<uint64_t>(left_ms, 10));
    }
}

int main(int argc, char** argv) {
    SetConsoleOutputCP(CP_UTF8);
    SetConsoleCP(CP_UTF8);

    std::string midi_file = "test.mid";
    if (argc >= 2) {
        midi_file = argv[1];
    }

    try {
        MidiSong song = ReadMidi(midi_file);
        if (song.events.empty()) {
            std::cerr << "В MIDI не найдено нот.\n";
            return 1;
        }

        if (!InitInpOut()) {
            return 1;
        }

        std::cout << "Играю " << midi_file << " через PC speaker. Событий: "
                  << song.events.size() << ". Ctrl+C для остановки.\n";

        std::map<uint8_t, int> active_notes;
        uint64_t start_tick = GetTickCount64();

        for (size_t i = 0; i < song.events.size(); ++i) {
            const MidiEvent& e = song.events[i];
            SleepUntil(e.time_us, start_tick);

            if (e.is_on) {
                active_notes[e.note]++;
            } else {
                std::map<uint8_t, int>::iterator it = active_notes.find(e.note);
                if (it != active_notes.end()) {
                    if (--it->second <= 0) {
                        active_notes.erase(it);
                    }
                }
            }

            if (active_notes.empty()) {
                SilenceSpeaker();
            } else {
                int note = active_notes.rbegin()->first;
                SetSpeakerFrequency((unsigned int)(MidiNoteToFrequency(note) + 0.5));
            }
        }

        SilenceSpeaker();
        CloseInpOut();
        std::cout << "Готово.\n";
        return 0;
    } catch (const std::exception& ex) {
        SilenceSpeaker();
        CloseInpOut();
        std::cerr << "Ошибка: " << ex.what() << "\n";
        return 1;
    }
}
