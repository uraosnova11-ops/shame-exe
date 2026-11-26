"""Small native Windows shell for the local YuriCRM web interface."""
import ctypes
import sys
import threading
import traceback
import webbrowser
from ctypes import wintypes

import app


class POINT(ctypes.Structure):
    _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [('hwnd', wintypes.HWND), ('message', wintypes.UINT),
                ('wParam', ctypes.c_size_t), ('lParam', ctypes.c_ssize_t),
                ('time', wintypes.DWORD), ('pt', POINT), ('lPrivate', wintypes.DWORD)]


def window():
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    kernel32.GetModuleHandleW.restype = wintypes.HINSTANCE
    user32.CreateWindowExW.restype = wintypes.HWND
    user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR,
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU,
        wintypes.HINSTANCE, ctypes.c_void_p]
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND,
                                   wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = wintypes.BOOL
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
    user32.DispatchMessageW.restype = ctypes.c_ssize_t
    instance = kernel32.GetModuleHandleW(None)
    style = 0x00CF0000 | 0x10000000  # WS_OVERLAPPEDWINDOW | WS_VISIBLE
    hwnd = user32.CreateWindowExW(0, 'STATIC', 'Юрий — локальные клиенты',
                                   style, 180, 150, 480, 230, None, None,
                                   instance, None)
    if not hwnd:
        raise OSError('Не удалось открыть окно приложения')
    child_style = 0x40000000 | 0x10000000  # WS_CHILD | WS_VISIBLE
    user32.CreateWindowExW(0, 'STATIC',
        'Панель работает в браузере. Закрытие окна остановит фоновые задачи.',
        child_style, 22, 35, 425, 55, hwnd, None, instance, None)
    user32.CreateWindowExW(0, 'BUTTON', 'Открыть панель',
        child_style | 0x00000001, 22, 105, 175, 38, hwnd,
        ctypes.c_void_p(100), instance, None)
    return user32, hwnd


def main():
    if '--smoke' in sys.argv:
        server = app.run_server()
        try:
            server.serve_forever()
        finally:
            app.STOP.set()
            server.server_close()
        return

    user32, hwnd = window()
    if '--gui-smoke' in sys.argv:
        user32.DestroyWindow(hwnd)
        return
    try:
        server = app.run_server()
    except Exception:
        user32.MessageBoxW(hwnd,
            'Не удалось открыть локальную панель. Закройте другой экземпляр и запустите приложение снова.',
            'Ошибка запуска', 0x10)
        user32.DestroyWindow(hwnd)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f'http://{app.HOST}:{app.PORT}'
    webbrowser.open(url)
    try:
        message = MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.hwnd == hwnd and message.message == 0x10:  # WM_CLOSE
                break
            if message.hwnd == hwnd and message.message == 0x111 and message.wParam & 0xffff == 100:
                webbrowser.open(url)
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    finally:
        app.STOP.set()
        server.shutdown()
        server.server_close()
        user32.DestroyWindow(hwnd)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        app.DATA.mkdir(parents=True, exist_ok=True)
        frames = '\n'.join(f'{f.filename}:{f.lineno} in {f.name}'
                           for f in traceback.extract_tb(exc.__traceback__))
        (app.DATA / 'startup-error.txt').write_text(
            f'{type(exc).__name__}: {exc}\n{frames}', encoding='utf-8')
        raise
