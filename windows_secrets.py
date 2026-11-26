"""Protect locally stored credentials for the current Windows user with DPAPI."""
import ctypes
import os
from ctypes import wintypes

class SecretStorageError(ValueError): pass

class DATA_BLOB(ctypes.Structure):
    _fields_=[('cbData',wintypes.DWORD),('pbData',ctypes.POINTER(ctypes.c_byte))]

if os.name=='nt':
    crypt=ctypes.WinDLL('crypt32',use_last_error=True)
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    for name in ('CryptProtectData','CryptUnprotectData'):
        fn=getattr(crypt,name);fn.restype=wintypes.BOOL
        fn.argtypes=[ctypes.POINTER(DATA_BLOB),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(DATA_BLOB)]
    kernel.LocalFree.argtypes=[ctypes.c_void_p];kernel.LocalFree.restype=ctypes.c_void_p

def blob(data):
    buf=ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data),ctypes.cast(buf,ctypes.POINTER(ctypes.c_byte))),buf

def transform(data, decrypt=False):
    if os.name!='nt': raise SecretStorageError('Защищённое хранилище доступно только в Windows.')
    source,keep=blob(data);result=DATA_BLOB()
    method=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    if not method(ctypes.byref(source),None,None,None,None,0,ctypes.byref(result)):
        raise SecretStorageError('Windows не смогла открыть защищённые настройки. Запустите приложение под прежней учётной записью Windows.')
    try: return ctypes.string_at(result.pbData,result.cbData)
    finally: kernel.LocalFree(result.pbData)

def protect(data): return transform(data)
def unprotect(data): return transform(data,True)
