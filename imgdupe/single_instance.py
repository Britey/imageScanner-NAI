from __future__ import annotations

import ctypes
import os
import tempfile
from pathlib import Path


DEFAULT_INSTANCE_NAME = "imgdupe-web-ui-single-instance"
ERROR_ALREADY_EXISTS = 183


class SingleInstanceLock:
    def __init__(self, name: str = DEFAULT_INSTANCE_NAME) -> None:
        self.name = name
        self._handle = None
        self._file = None

    def acquire(self) -> bool:
        if self._handle is not None or self._file is not None:
            return True
        if os.name == "nt":
            return self._acquire_windows_mutex()
        return self._acquire_file_lock()

    def release(self) -> None:
        if self._handle is not None:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_int
            kernel32.CloseHandle(self._handle)
            self._handle = None
        if self._file is not None:
            try:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            finally:
                self._file.close()
                self._file = None

    def _acquire_windows_mutex(self) -> bool:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        ctypes.set_last_error(0)
        handle = kernel32.CreateMutexW(None, False, f"Local\\{self.name}")
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def _acquire_file_lock(self) -> bool:
        import fcntl

        safe_name = "".join(character if character.isalnum() else "-" for character in self.name)
        lock_path = Path(tempfile.gettempdir()) / f"{safe_name}.lock"
        lock_file = lock_path.open("a+b")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return False
        self._file = lock_file
        return True


def notify_already_running() -> None:
    message = "Another copy of imgdupe is already running. This launch will now close."
    print(message)
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.MessageBoxW(None, message, "imgdupe is already running", 0x30)
    except Exception:
        pass
