import subprocess
import sys
import unittest
from unittest.mock import patch

from core import child_process


class ChildCommandTests(unittest.TestCase):
    def test_source_command_shape(self):
        command = child_process.python_command("core.export_queue", "/tmp/job", 7)
        self.assertEqual(command[0], sys.executable)
        self.assertEqual(command[1:4], ["-u", "-m", "core.export_queue"])
        self.assertEqual(command[4:], ["/tmp/job", "7"])

    def test_frozen_command_uses_the_dispatch_flag(self):
        with patch.object(sys, "frozen", True, create=True):
            command = child_process.python_command("core.media_download", "/tmp/job")
        self.assertEqual(command[:4], [sys.executable, "--run-module", "core.media_download", "/tmp/job"])

    def test_source_command_actually_runs(self):
        result = subprocess.run(child_process.python_command("core.child_process"),
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr[-300:])

    def test_dispatch_runs_the_module_and_sets_argv(self):
        calls = {}

        def fake_run_module(module, run_name=None, alter_sys=False):
            calls.update(module=module, run_name=run_name, alter_sys=alter_sys, argv=list(sys.argv))

        argv = ["app.exe", "--run-module", "core.export_queue", "job-1", "x"]
        with patch("runpy.run_module", side_effect=fake_run_module):
            handled = child_process.dispatch(argv)
        self.assertTrue(handled)
        self.assertEqual(calls["module"], "core.export_queue")
        self.assertEqual(calls["run_name"], "__main__")
        self.assertEqual(calls["argv"], ["core.export_queue", "job-1", "x"])

    def test_ensure_stdio_recovers_from_none(self):
        saved = (sys.stdout, sys.stderr)
        try:
            sys.stdout = None
            sys.stderr = None
            child_process.ensure_stdio()
            self.assertTrue(hasattr(sys.stdout, "write"))
            self.assertTrue(hasattr(sys.stderr, "write"))
        finally:
            sys.stdout, sys.stderr = saved

    def test_dispatch_ignores_normal_launches(self):
        self.assertFalse(child_process.dispatch(["app.exe"]))
        self.assertFalse(child_process.dispatch(["app.exe", "--smoke"]))
        self.assertFalse(child_process.dispatch(["app.exe", "--run-module"]))

    def test_child_env_is_utf8_and_unbuffered(self):
        env = child_process.child_env({"KEEP": "1", "PYTHONUNBUFFERED": "0"})
        self.assertEqual(env["KEEP"], "1")
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")


if __name__ == "__main__":
    unittest.main()
