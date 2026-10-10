import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from tools import Tools


class SandboxTest(unittest.TestCase):
    def test_commands_and_scripts_cannot_read_sibling_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            root, requirements, private = (parent / p for p in ("app", "requirements", "private"))
            for p in (root, requirements, private):
                p.mkdir()
            (private / "record.txt").write_text("outside-workspace-sentinel")
            (requirements / "public.txt").write_text("public contract")
            (root / "escape").symlink_to(private, target_is_directory=True)
            script = root / "probe.py"
            script.write_text(
                "from pathlib import Path\n"
                "for p in [Path('../private/record.txt'), Path('escape/record.txt')]:\n"
                "    try: p.read_text()\n"
                "    except PermissionError: print('denied')\n"
                "    else: raise AssertionError('out-of-workspace read succeeded')\n"
                "try: Path('../private/new.txt').write_text('bad')\n"
                "except PermissionError: print('write denied')\n"
                "else: raise AssertionError('out-of-workspace write succeeded')\n"
                "Path('result.txt').write_text('inside')\n"
            )
            tool = Tools(root, readable=[requirements])

            def run(command):
                return tool.run("bash", json.dumps({"command": command}))

            out = run(f"{shlex.quote(sys.executable)} probe.py")
            self.assertIn("[exit 0]", out, out)
            self.assertEqual(out.count("denied"), 3, out)
            self.assertEqual((root / "result.txt").read_text(), "inside")
            out = run(f"cat {shlex.quote(str(requirements / 'public.txt'))}")
            self.assertIn("public contract", out)
            out = run("ls ../private")
            self.assertNotIn("record.txt", out)
            self.assertNotIn("[exit 0]", out)
            out = run(f"printf changed > {shlex.quote(str(requirements / 'public.txt'))}")
            self.assertNotIn("[exit 0]", out)
            self.assertEqual((requirements / "public.txt").read_text(), "public contract")

    def test_missing_sandbox_refuses_the_command(self):
        from unittest.mock import patch

        import sandbox

        with (
            patch.object(sandbox.sys, "platform", "unsupported"),
            patch.object(sandbox.sys, "argv", ["sandbox.py", '{"read": [], "write": []}', "true"]),
            self.assertRaisesRegex(RuntimeError, "no filesystem sandbox"),
        ):
            sandbox.main()

    @unittest.skipUnless(sys.platform == "linux", "procfs is Linux-specific")
    def test_children_can_read_their_own_process_data_but_not_the_controller(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "process.py").write_text(
                "from pathlib import Path\n"
                "assert any(Path('/proc').iterdir())\n"
                "assert Path('/proc/self/stat').read_text()\n"
                f"try: Path('/proc/{os.getpid()}/environ').read_bytes()\n"
                "except PermissionError: print('controller denied')\n"
                "else: raise AssertionError('controller environment was readable')\n"
            )
            out = Tools(root).run("bash", json.dumps({"command": f"{shlex.quote(sys.executable)} process.py"}))
            self.assertIn("[exit 0]", out, out)
            self.assertIn("controller denied", out, out)


if __name__ == "__main__":
    unittest.main()
