"""Native YuriCRM window. The HTTP service is private to this process."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback

if '--smoke' not in sys.argv:
    os.environ.setdefault('YURI_PORT','0')

import app


def ensure_webview_runtime():
    """Install the bundled Microsoft x64 offline runtime for this Windows user if needed."""
    import winreg
    product=r'{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
    locations=((winreg.HKEY_LOCAL_MACHINE,rf'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{product}'),
               (winreg.HKEY_CURRENT_USER,rf'Software\Microsoft\EdgeUpdate\Clients\{product}'))
    def installed():
        for hive,path in locations:
            try:
                with winreg.OpenKey(hive,path) as key:
                    version=winreg.QueryValueEx(key,'pv')[0]
                    if version and version!='0.0.0.0': return True
            except OSError: pass
        return False
    if installed(): return
    path=Path(getattr(sys,'_MEIPASS',Path(__file__).resolve().parent))/'MicrosoftEdgeWebView2RuntimeInstallerX64.exe'
    if not path.exists(): raise RuntimeError('В комплекте отсутствует компонент WebView2. Скачайте полную версию YuriCRM.')
    try:
        subprocess.run([str(path),'/silent','/install'],check=False,timeout=300,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except (OSError,subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Не удалось установить встроенный компонент WebView2. Проверьте права учётной записи Windows.') from exc
    if not installed(): raise RuntimeError('Windows не завершила установку WebView2. Перезагрузите компьютер и запустите YuriCRM снова.')


def main():
    server=app.run_server()
    thread=threading.Thread(target=server.serve_forever,name='YuriCRM-HTTP',daemon=True)
    thread.start()
    try:
        if '--smoke' in sys.argv:
            thread.join()
            return
        ensure_webview_runtime()
        if '--gui-smoke' in sys.argv:
            (app.DATA/'gui-smoke-ok.txt').unlink(missing_ok=True)
            (app.DATA/'YuriCRM-window.png').unlink(missing_ok=True)
            (app.DATA/'gui-error.txt').unlink(missing_ok=True)
        import webview
        url=f'http://127.0.0.1:{server.server_address[1]}/'
        window=webview.create_window('YuriCRM — поиск клиентов',url,width=1320,height=860,
                                     min_size=(880,620),background_color='#f5f7f8',
                                     text_select=True,confirm_close=False)
        def gui_smoke():
            if '--gui-smoke' not in sys.argv: return
            exit_code=1
            try:
                ready=False
                diagnostic=''
                for _ in range(60):
                    try:
                        result=window.evaluate_js("""(() => { try {
                          if (!document.querySelector('.nav-item') && typeof desktopInit === 'function') desktopInit();
                          return {ready: document.querySelectorAll('.nav-item').length === 6,
                                  state: document.readyState, url: location.href,
                                  text: (document.body?.innerText || '').slice(0, 500)};
                        } catch (e) { return {ready:false,error:String(e && (e.stack || e))}; } })()""")
                        ready=bool(result and result.get('ready'))
                        diagnostic=str(result)
                        if ready: break
                    except Exception: pass
                    time.sleep(.5)
                if not ready: raise RuntimeError('Главные экраны не открылись во встроенном окне: '+diagnostic)
                screens=window.evaluate_js("['companies','analysis','demos','messages','campaign','settings'].every(k => { desktopNavigate(k); return document.querySelector('.desktop-page.active').dataset.page === k })")
                if not screens: raise RuntimeError('Не все экраны приложения доступны')
                window.evaluate_js("desktopNavigate('companies')")
                active=window.evaluate_js("Array.from(document.querySelectorAll('.nav-item.active')).map(el=>el.dataset.target).join(',')")
                if active!='companies': raise RuntimeError('Боковое меню не переключилось на компании')
                time.sleep(.8)  # Let WebView2 paint after the navigation test.
                # Capture the actual rendered WebView2 surface inside the native window.
                import System
                from System.IO import MemoryStream
                from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat
                stream=MemoryStream();tasks=[]
                def capture():
                    tasks.append(window.native.webview.CoreWebView2.CapturePreviewAsync(
                        CoreWebView2CapturePreviewImageFormat.Png,stream))
                window.native.Invoke(System.Action(capture))
                if not tasks or not tasks[0].Wait(15000): raise RuntimeError('Не удалось снять изображение встроенного окна')
                png=bytes(stream.ToArray())
                if not png.startswith(b'\x89PNG') or len(png)<10000: raise RuntimeError('Встроенное окно вернуло пустой снимок')
                (app.DATA/'YuriCRM-window.png').write_bytes(png)
                (app.DATA/'gui-smoke-ok.txt').write_text('Встроенное окно и интерфейс открыты',encoding='utf-8')
                exit_code=0
                time.sleep(2)
            except Exception:
                (app.DATA/'gui-error.txt').write_text(traceback.format_exc(),encoding='utf-8')
            finally:
                window.destroy()
                # PyWebView can keep its WinForms message loop alive after the
                # window has been destroyed on a headless Actions desktop.
                # This branch is used only by the packaged-EXE smoke test;
                # the PNG and success marker above prove that the real WebView
                # rendered and all screens were reachable before we exit.
                time.sleep(.5)
                os._exit(exit_code)
        webview.start(gui_smoke,gui='edgechromium',debug=False)
        if '--gui-smoke' in sys.argv and not (app.DATA/'gui-smoke-ok.txt').exists():
            detail=(app.DATA/'gui-error.txt').read_text(encoding='utf-8') if (app.DATA/'gui-error.txt').exists() else ''
            raise RuntimeError('Встроенный интерфейс не прошёл проверку. '+detail)
    finally:
        app.STOP.set()
        app.TG.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        app.DATA.mkdir(parents=True,exist_ok=True)
        frames='\n'.join(f'{f.filename}:{f.lineno} in {f.name}' for f in traceback.extract_tb(exc.__traceback__))
        (app.DATA/'startup-error.txt').write_text(f'{type(exc).__name__}: {exc}\n{frames}',encoding='utf-8')
        if '--smoke' not in sys.argv and '--gui-smoke' not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,
                'Не удалось открыть окно YuriCRM. Проверьте, что Microsoft Edge WebView2 установлен, и повторите запуск. Подробности: %LOCALAPPDATA%\\YuriCRM\\startup-error.txt',
                'YuriCRM — ошибка запуска',0x10)
        sys.exit(1)

