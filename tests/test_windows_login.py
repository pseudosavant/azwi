"""Real Windows console cancellation with a fake Azure CLI. No account access."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest


DRIVER = '''
import ctypes
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from azwi.azure_cli import AzureCLI
from azwi.errors import AuthError
root = Path(sys.argv[2])
ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
(root / "driver.pid").write_text(str(os.getpid()))
cli = AzureCLI(env=os.environ, stdin=sys.stdin, stderr=sys.stderr)
cli.executable = lambda: str(root / "fake-az.cmd")
try:
    cli.login("11111111-1111-4111-8111-111111111111", "device-code")
except AuthError as error:
    print(str(error), file=sys.stderr, flush=True)
    sys.exit(error.exit_code)
'''

FAKE_CLI = '''
import os
from pathlib import Path
import time
Path(__file__).with_suffix(".pid").write_text(str(os.getpid()))
time.sleep(120)
'''

SIGNAL = '''
import ctypes
import sys
import time
k = ctypes.WinDLL("kernel32", use_last_error=True)
k.FreeConsole()
if not k.AttachConsole(int(sys.argv[1])):
    raise ctypes.WinError(ctypes.get_last_error())
k.SetConsoleCtrlHandler(None, True)
if not k.GenerateConsoleCtrlEvent(0, 0):
    raise ctypes.WinError(ctypes.get_last_error())
time.sleep(0.5)
k.FreeConsole()
'''


@unittest.skipUnless(os.name == "nt" and shutil.which("uv"), "Windows console and uv required")
class WindowsLoginTests(unittest.TestCase):
    def test_ctrl_c_through_uv_stops_login_process_tree(self):
        self.check_cancellation()

    @unittest.skipUnless(shutil.which("pwsh"), "PowerShell required")
    def test_ctrl_c_through_powershell_stops_login_process_tree(self):
        self.check_cancellation(powershell=True)

    def test_abrupt_parent_exit_stops_login_process_tree(self):
        self.check_cancellation(force_exit=True)

    def check_cancellation(self, *, powershell=False, force_exit=False):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with tempfile.TemporaryDirectory(prefix="azwi-console-test-") as directory:
            root = Path(directory)
            (root / "driver.py").write_text(DRIVER)
            (root / "fake.py").write_text(FAKE_CLI)
            (root / "fake-az.cmd").write_text(f'@echo off\n"{sys.executable}" "{root / "fake.py"}" %*\n')
            startup = subprocess.STARTUPINFO()
            startup.dwFlags = subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            child_handle = None
            driver_handle = None
            command = [shutil.which("uv"), "run", "--no-project", "--no-config", "--offline",
                       "--python", sys.executable, str(root / "driver.py"),
                       str(Path(__file__).resolve().parents[1] / "src"), str(root)]
            if powershell:
                script = "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in command)
                command = [shutil.which("pwsh"), "-NoProfile", "-Command", script]
            with (root / "output.txt").open("w+") as output:
                process = subprocess.Popen(
                    command,
                    cwd=root, stdout=output, stderr=output,
                    env={**os.environ, "UV_CACHE_DIR": str(root / "uv-cache")},
                    creationflags=subprocess.CREATE_NEW_CONSOLE, startupinfo=startup,
                )
                try:
                    deadline = time.monotonic() + 20
                    while not (root / "fake.pid").exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(0.05)
                    output.seek(0)
                    self.assertTrue((root / "fake.pid").exists(), output.read())
                    child_handle = kernel.OpenProcess(0x100001, False, int((root / "fake.pid").read_text()))
                    self.assertTrue(child_handle)
                    if force_exit:
                        driver_handle = kernel.OpenProcess(0x100001, False, int((root / "driver.pid").read_text()))
                        self.assertTrue(driver_handle)
                        self.assertTrue(kernel.TerminateProcess(driver_handle, 99))
                    else:
                        sent = subprocess.run([sys.executable, "-c", SIGNAL, str(process.pid)],
                                              capture_output=True, text=True, timeout=10)
                        self.assertEqual(sent.returncode, 0, sent.stderr)
                    try:
                        code = process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        output.seek(0)
                        self.fail("azwi did not respond to Ctrl+C within fifteen seconds: " + output.read())
                    output.seek(0)
                    message = output.read()
                    self.assertEqual(kernel.WaitForSingleObject(child_handle, 3000), 0,
                                     f"Azure CLI descendant survived cancellation. Exit code: {code}. Output: {message}")
                    if force_exit:
                        self.assertNotEqual(code, 0)
                    else:
                        # pwsh -Command reports exit code 1 when Ctrl+C interrupts it.
                        self.assertEqual(code, 1 if powershell else 4, message)
                        self.assertIn("cancelled", message)
                    self.assertNotIn("Traceback", message)
                finally:
                    # Terminate only this test's known process tree and held child handle.
                    if driver_handle:
                        if kernel.WaitForSingleObject(driver_handle, 0) != 0:
                            kernel.TerminateProcess(driver_handle, 1)
                            kernel.WaitForSingleObject(driver_handle, 5000)
                        kernel.CloseHandle(driver_handle)
                    if child_handle:
                        if kernel.WaitForSingleObject(child_handle, 0) != 0:
                            kernel.TerminateProcess(child_handle, 1)
                            kernel.WaitForSingleObject(child_handle, 5000)
                        kernel.CloseHandle(child_handle)
                    if process.poll() is None:
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       capture_output=True, timeout=10)
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
