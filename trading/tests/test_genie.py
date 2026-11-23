"""Genie (web/api/genie.js) understands whole questions: runs web/tests/genie_matrix.js (750+ questions: one word, typos,
stock x topic, comparisons, screens, glossary, Hinglish). Needs Node; skipped without it."""
import shutil, subprocess, unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web"


class GenieMatrix(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_every_question_is_understood(self):
        r = subprocess.run(["node", str(WEB / "tests" / "genie_matrix.js")], capture_output=True, text=True, timeout=600)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("FAILED 0", r.stdout)


if __name__ == "__main__":
    unittest.main()
