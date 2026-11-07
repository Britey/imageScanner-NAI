from __future__ import annotations

import contextlib
import io
import itertools
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from imgdupe import single_instance as si


REPOSITORY = Path(__file__).resolve().parents[1]
LOCK_HOLDER = (
    "import sys\n"
    "from imgdupe.single_instance import SingleInstanceLock\n"
    "lock = SingleInstanceLock(sys.argv[1])\n"
    "print('acquired' if lock.acquire() else 'busy', flush=True)\n"
    "sys.stdin.readline()\n"
)


@unittest.skipIf(os.name == "nt", "POSIX file locks; the Win32 mutex is tested below")
class FileLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        patcher = patch.object(si.tempfile, "gettempdir", return_value=str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)

    def lock(self, name="imgdupe test / lock"):
        lock = si.SingleInstanceLock(name)
        self.addCleanup(lock.release)
        return lock

    def test_only_one_holder_until_release_and_reacquire_is_idempotent(self):
        first, second = self.lock(), self.lock()
        self.assertTrue(first.acquire())
        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        self.assertTrue(self.lock("other instance").acquire())
        first.release()
        self.assertTrue(second.acquire())
        self.assertEqual(sorted(path.name for path in self.root.iterdir()),
                         ["imgdupe-test---lock.lock", "other-instance.lock"])

    def test_lock_held_by_another_process_blocks_until_that_process_exits(self):
        holder = subprocess.Popen(
            [sys.executable, "-c", LOCK_HOLDER, "shared"], cwd=self.root,
            env=dict(os.environ, TMPDIR=str(self.root), PYTHONPATH=str(REPOSITORY)),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(holder.wait, 10)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "acquired")
        lock = self.lock("shared")
        self.assertFalse(lock.acquire())
        holder.communicate("\n", timeout=10)
        self.assertTrue(lock.acquire())


class FakeKernel32:
    def __init__(self):
        self.last_error = 0
        self.create_error = None
        self.open_handles = {}
        self._handles = itertools.count(100)
        self.CreateMutexW = Mock(side_effect=self._create_mutex)
        self.CloseHandle = Mock(side_effect=self._close_handle)

    def _create_mutex(self, _attributes, _initial_owner, name):
        if self.create_error is not None:
            self.last_error = self.create_error
            return None
        self.last_error = si.ERROR_ALREADY_EXISTS if name in self.open_handles.values() else 0
        handle = next(self._handles)
        self.open_handles[handle] = name
        return handle

    def _close_handle(self, handle):
        del self.open_handles[handle]
        return 1


class WindowsMutexTests(unittest.TestCase):
    def setUp(self):
        self.kernel = FakeKernel32()
        ctypes = si.ctypes
        for name, replacement in (
            ("WinDLL", Mock(return_value=self.kernel)),
            ("set_last_error", Mock(side_effect=lambda code: setattr(self.kernel, "last_error", code))),
            ("get_last_error", Mock(side_effect=lambda: self.kernel.last_error)),
            ("WinError", Mock(side_effect=lambda code: OSError(code, "Windows error"))),
        ):
            patcher = patch.object(ctypes, name, replacement, create=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(si, "os", SimpleNamespace(name="nt"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_named_session_mutex_allows_one_holder_until_release(self):
        first, second = si.SingleInstanceLock("instance"), si.SingleInstanceLock("instance")
        self.assertTrue(first.acquire())
        self.assertTrue(first.acquire())
        self.assertEqual(list(self.kernel.open_handles.values()), ["Local\\instance"])
        self.assertFalse(second.acquire())
        self.assertEqual(list(self.kernel.open_handles.values()), ["Local\\instance"])
        first.release()
        self.assertEqual(self.kernel.open_handles, {})
        self.assertTrue(second.acquire())
        second.release()
        self.assertEqual(self.kernel.open_handles, {})

    def test_default_name_does_not_depend_on_install_folder(self):
        lock = si.SingleInstanceLock()
        self.assertTrue(lock.acquire())
        self.assertEqual(list(self.kernel.open_handles.values()), [f"Local\\{si.DEFAULT_INSTANCE_NAME}"])
        lock.release()

    def test_mutex_creation_failure_raises_and_holds_nothing(self):
        self.kernel.create_error = 5
        lock = si.SingleInstanceLock("instance")
        with self.assertRaises(OSError) as raised:
            lock.acquire()
        self.assertEqual(raised.exception.errno, 5)
        self.kernel.create_error = None
        self.assertTrue(lock.acquire())
        lock.release()


class AlreadyRunningNotificationTests(unittest.TestCase):
    message = "Another copy of imgdupe is already running. This launch will now close."

    def notify(self, os_name, windll):
        with patch.object(si, "os", SimpleNamespace(name=os_name)), patch.object(
            si.ctypes, "windll", windll, create=True
        ), contextlib.redirect_stdout(io.StringIO()) as output:
            si.notify_already_running()
        return output.getvalue()

    def test_posix_prints_warning_without_a_dialog(self):
        windll = Mock()
        self.assertEqual(self.notify("posix", windll), self.message + "\n")
        windll.user32.MessageBoxW.assert_not_called()

    def test_windows_also_shows_a_warning_dialog(self):
        windll = Mock()
        self.assertEqual(self.notify("nt", windll), self.message + "\n")
        windll.user32.MessageBoxW.assert_called_once_with(None, self.message, "imgdupe is already running", 0x30)

    def test_windows_dialog_failure_still_exits_quietly(self):
        windll = Mock()
        windll.user32.MessageBoxW.side_effect = OSError("no desktop")
        self.assertEqual(self.notify("nt", windll), self.message + "\n")


if __name__ == "__main__":
    unittest.main()
