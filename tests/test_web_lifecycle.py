from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

from imgdupe import web
from imgdupe.settings import AppSettings
from web_handler_fixture import WebFixture


class WebLifecycleTests(WebFixture):
    def test_serve_closes_server_and_removes_workdir(self):
        for browser, checkpoint in [
            (False, None),
            (True, (1, 2, 3)),
            (False, (0, 0, 0)),
        ]:
            with self.subTest(browser=browser, checkpoint=checkpoint):
                work = self.root / f"serve-{browser}-{checkpoint}"
                server = Mock(server_port=8765)
                with (
                    patch.object(web.tempfile, "mkdtemp", return_value=str(work)),
                    patch.object(web, "HTTPServer", return_value=server),
                    patch.object(web, "checkpoint_database", return_value=checkpoint),
                    patch.object(web.webbrowser, "open") as opened,
                    patch.object(web, "load_settings", return_value=AppSettings()),
                ):
                    url = web.serve(
                        self.conn,
                        self.state.db_path,
                        open_browser=browser,
                        settings=AppSettings() if browser else None,
                    )
                self.assertEqual(url, "http://127.0.0.1:8765")
                server.serve_forever.assert_called_once()
                server.server_close.assert_called_once()
                self.assertEqual(opened.call_count, int(browser))
                self.assertFalse(work.exists())

    def test_serve_interrupt_still_closes_and_cleans_up(self):
        work = self.root / "interrupted-server"
        server = Mock(server_port=8765)
        server.serve_forever.side_effect = KeyboardInterrupt()
        with (
            patch.object(web.tempfile, "mkdtemp", return_value=str(work)),
            patch.object(web, "HTTPServer", return_value=server),
            patch.object(
                web, "checkpoint_database", return_value=(0, 0, 0)
            ) as checkpoint,
            patch.object(web.WebState, "wait_for_scan") as wait,
        ):
            with self.assertRaises(KeyboardInterrupt):
                web.serve(self.conn, self.state.db_path, settings=AppSettings())
        server.server_close.assert_called_once()
        wait.assert_called_once()
        checkpoint.assert_called_once_with(self.conn, "TRUNCATE")
        self.assertFalse(work.exists())


class FolderIntegrationTests(WebFixture):
    def test_show_in_folder_windows_api_and_fallback(self):
        path = self.image()
        windows = SimpleNamespace(name="nt")
        with (
            patch.object(web, "os", windows),
            patch.object(web, "_sh_open_folder_and_select") as select,
            patch.object(web.subprocess, "Popen") as popen,
        ):
            web._show_path_in_folder(path)
            select.assert_called_once_with(path)
            popen.assert_not_called()
        with (
            patch.object(web, "os", windows),
            patch.object(
                web, "_sh_open_folder_and_select", side_effect=OSError("shell")
            ),
            patch.object(web.subprocess, "Popen") as popen,
        ):
            web._show_path_in_folder(path)
            self.assertEqual(
                popen.call_args.args[0], ["explorer.exe", str(path.parent)]
            )
        with (
            patch.object(web, "os", SimpleNamespace(name="posix")),
            patch.object(web.subprocess, "Popen") as popen,
        ):
            web._show_path_in_folder(path)
            web._show_path_in_folder(self.root / "missing.png")
            self.assertEqual(popen.call_count, 2)
        with (
            patch.object(web, "os", windows),
            patch.object(web.subprocess, "Popen") as popen,
        ):
            web._show_path_in_folder(self.root / "missing.png")
            popen.assert_called_once()

    def test_shell_api_releases_allocations_and_initialization(self):
        for parse_result, open_result, initialized, allocated in [
            (0, 0, 0, True),
            # RPC_E_CHANGED_MODE: selection may succeed using an existing COM
            # apartment, but this call must not uninitialize an apartment it
            # did not initialize itself.
            (0, 0, -2147417850, True),
            (1, 0, 1, False),
            (0, 1, -1, True),
        ]:
            with self.subTest(parse_result=parse_result, open_result=open_result):
                ole = Mock()
                shell = Mock()
                ole.CoInitialize.return_value = initialized
    
                def parse(path, unused, pidl, flags, sfgao):
                    if allocated:
                        pidl._obj.value = 123
                    return parse_result
    
                shell.SHParseDisplayName.side_effect = parse
                shell.SHOpenFolderAndSelectItems.return_value = open_result
                with (
                    patch.object(
                        web.ctypes, "OleDLL", side_effect=[ole, shell], create=True
                    ),
                    patch.object(
                        web.ctypes,
                        "WinError",
                        side_effect=lambda value: OSError(f"win error {value}"),
                        create=True,
                    ),
                ):
                    if parse_result or open_result:
                        with self.assertRaisesRegex(OSError, "win error"):
                            web._sh_open_folder_and_select(self.root)
                    else:
                        web._sh_open_folder_and_select(self.root)
                self.assertEqual(ole.CoTaskMemFree.call_count, int(allocated))
                self.assertEqual(
                    ole.CoUninitialize.call_count, int(initialized in (0, 1))
                )

