"""Windowed entry point for a future bundled Windows executable."""
import threading
import webbrowser
import sys
import tkinter as tk
import traceback
from tkinter import messagebox
import app

def main():
    if '--smoke' in sys.argv:
        server=app.run_server()
        try: server.serve_forever()
        finally: app.STOP.set();server.server_close()
        return
    if '--gui-smoke' in sys.argv:
        root=tk.Tk();root.update();root.destroy()
        return
    root=tk.Tk();root.title('Юрий · локальные клиенты');root.geometry('450x230')
    root.configure(bg='#f5f4f0')
    try: server=app.run_server()
    except Exception:
        messagebox.showerror('Не удалось запустить','Приложение не смогло открыть локальный интерфейс. Закройте другой экземпляр и запустите снова.')
        root.destroy();return
    threading.Thread(target=server.serve_forever,daemon=True).start()
    url=f'http://{app.HOST}:{app.PORT}'
    tk.Label(root,text='Юрий · локальные клиенты',font=('Segoe UI',19,'bold'),bg='#f5f4f0',fg='#1b302a').pack(pady=(28,8))
    tk.Label(root,text='Панель откроется в браузере.\nЗакрытие этого окна останавливает фоновые задачи.',font=('Segoe UI',10),bg='#f5f4f0',fg='#68736e').pack(pady=8)
    tk.Button(root,text='Открыть панель',font=('Segoe UI',11),command=lambda:webbrowser.open(url),bg='#2f7052',fg='white',padx=20,pady=7).pack(pady=8)
    def close():
        app.STOP.set();server.shutdown();server.server_close();root.destroy()
    root.protocol('WM_DELETE_WINDOW',close)
    root.after(350,lambda:webbrowser.open(url))
    root.mainloop()

if __name__=='__main__':
    try: main()
    except Exception as exc:
        from pathlib import Path
        app.DATA.mkdir(parents=True, exist_ok=True)
        frames='\n'.join(f'{f.filename}:{f.lineno} in {f.name}' for f in traceback.extract_tb(exc.__traceback__))
        (app.DATA/'startup-error.txt').write_text(f'{type(exc).__name__}: {exc}\n{frames}', encoding='utf-8')
        raise
