#include <iostream>
#include <windows.h>

typedef void (WINAPI *TYPE_OUT32)(short PortAddress, short data);
typedef short (WINAPI *TYPE_INP32)(short PortAddress);
typedef BOOL (WINAPI *TYPE_ISOPEN)(void);

TYPE_OUT32 pOut32 = NULL;
TYPE_INP32 pInp32 = NULL;
TYPE_ISOPEN pIsInpOutDriverOpen = NULL;
HMODULE hInpOutDll = NULL; 

// Порты I/O
#define TIMER_CMD_PORT      0x43
#define TIMER_DATA_PORT     0x42
#define PC_SPEAKER_PORT     0x61

void micro_delay(DWORD ms) {
    Sleep(ms);
}

BOOL InitInpOut() {
    hInpOutDll = LoadLibrary("inpoutx64.dll");
    if (hInpOutDll == NULL) {
        hInpOutDll = LoadLibrary("inpout32.dll");
    }

    if (hInpOutDll == NULL) {
        std::cout << "There isn't inpoutx64.dll or inpout32.dll!" << std::endl;
        return FALSE;
    }
    
    pOut32 = (TYPE_OUT32)GetProcAddress(hInpOutDll, "Out32");
    pInp32 = (TYPE_INP32)GetProcAddress(hInpOutDll, "Inp32");
    pIsInpOutDriverOpen = (TYPE_ISOPEN)GetProcAddress(hInpOutDll, "IsInpOutDriverOpen");

    if (!pOut32 || !pInp32 || !pIsInpOutDriverOpen) {
        std::cout << "Couldn't find all necessary functions in DLL." << std::endl;
        FreeLibrary(hInpOutDll);
        return FALSE;
    }

    return TRUE;
}


void Beep_Speaker(unsigned int frequency, DWORD duration_ms) {
    if (!pOut32 || !pInp32) return; 

    unsigned int divisor = 1193180 / frequency;

    pOut32(TIMER_CMD_PORT, 0xB6);
    pOut32(TIMER_DATA_PORT, (unsigned short)(divisor & 0xFF));
    pOut32(TIMER_DATA_PORT, (unsigned short)(divisor >> 8));

    unsigned short current_state = pInp32(PC_SPEAKER_PORT);
    unsigned short new_state = current_state | 0x03; 
    pOut32(PC_SPEAKER_PORT, new_state);

    micro_delay(duration_ms);

    new_state = current_state & 0xFC;
    pOut32(PC_SPEAKER_PORT, new_state);
}

// =======================================================
int main() {
    std::cout << "запустить DLL в Runtime" << std::endl;

    if (!InitInpOut()) {
        std::cout << "Критическая ошибка: не удалось инициализировать InpOut32/64." << std::endl;
        return 1;
    }
    
    if (pIsInpOutDriverOpen()) {
        std::cout << "Драйвер InpOut успешно открыт" << std::endl;
        
        // Нота Ля (440 Гц) на 500 мс
        Beep_Speaker(440, 500);
        micro_delay(200);
        
        // Нота До (523 Гц) на 500 мс
        Beep_Speaker(523, 500);
    } else {
        std::cout << "Драйвер InpOut не смог получить доступ к портам. Возможно, нет прав администратора" << std::endl;
    }
    
    if (hInpOutDll) {
        FreeLibrary(hInpOutDll);
    }

    std::cout << "Код выполнен" << std::endl;
    return 0;
}