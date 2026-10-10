import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))


class StartupBoundaryTest(unittest.TestCase):
    def test_generation_does_not_start_without_isolation(self):
        from unittest.mock import patch

        import main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            argv = ["main.py", str(root), "--output-dir", str(root)]
            with (
                patch.object(sys, "argv", argv),
                patch.object(main, "shell", return_value="SANDBOX ERROR: unavailable\n[exit 126]"),
                patch.object(main, "setup") as setup,
                patch.object(main.roles, "new_llm") as model,
            ):
                self.assertEqual(main.main(), 1)
                setup.assert_not_called()
                model.assert_not_called()

    def test_app_changes_and_checkpoints_are_published_without_private_artifacts(self):
        import checkpoint
        import main
        from tools import shell

        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            (output / "backend").mkdir()
            (output / "backend/old.js").write_text("old")
            (output / "backend/alias.js").symlink_to("old.js")
            (output / ".arc").mkdir()
            (output / ".arc/report.json").write_text("private sentinel")
            (output / ".gitignore").write_text(".arc/\n.agent/\n")
            shell("git init -q && git config user.email check@local && git config user.name check", output, 10)
            before = checkpoint.commit(output, "before")
            with main.isolated_workspace(output) as (root, publish):
                self.assertFalse((root / ".arc").exists())
                (root / "backend/old.js").unlink()
                (root / "backend/new.js").write_text("new")
                (root / "backend/alias.js").unlink()
                (root / "backend/alias.js").symlink_to("new.js")
                after = checkpoint.commit(root, "after")
                publish()
                self.assertFalse((output / "backend/old.js").exists())
                self.assertEqual((output / "backend/new.js").read_text(), "new")
                self.assertTrue((output / "backend/alias.js").is_symlink())
                (root / "backend/alias.js").unlink()
                (root / "backend/alias.js").write_text("standalone")
                publish()
                self.assertEqual((output / "backend/alias.js").read_text(), "standalone")
                self.assertEqual((output / "backend/new.js").read_text(), "new")
            self.assertEqual((output / ".arc/report.json").read_text(), "private sentinel")
            with main.isolated_workspace(output) as (root, publish):
                self.assertIn(after, shell("git rev-parse HEAD", root, 10))
                checkpoint.reset(root, before)
                publish()
                self.assertEqual((output / "backend/old.js").read_text(), "old")
                self.assertFalse((output / "backend/new.js").exists())


if __name__ == "__main__":
    unittest.main()
