import unittest

from pipeline_observability import PipelineProgress


class PipelineProgressTests(unittest.TestCase):
    def test_stage_diagnostic_progress_and_finish(self):
        messages = []
        progress = PipelineProgress(total=1, label="TEST", emit=messages.append)

        self.assertEqual(progress.run("load", lambda: 7), 7)
        progress.diagnostic("dataset", rows=3)
        progress.progress("history", 10, 20)
        progress.finish("SUCCESS")

        joined = "\n".join(messages)
        self.assertIn("[01/01] START load", joined)
        self.assertIn("[01/01] DONE  load | elapsed=", joined)
        self.assertIn("[DIAGNOSTIC] dataset | rows=3", joined)
        self.assertIn("[PROGRESS] history | 10/20", joined)
        self.assertIn("[TEST] SUCCESS | total_elapsed=", joined)

    def test_failure_is_logged_and_reraised(self):
        messages = []
        progress = PipelineProgress(total=1, emit=messages.append)

        with self.assertRaisesRegex(ValueError, "bad"):
            progress.run("parse", lambda: (_ for _ in ()).throw(ValueError("bad")))

        self.assertIn("[01/01] FAIL  parse | elapsed=", messages[-1])
        self.assertIn("error=ValueError", messages[-1])


if __name__ == "__main__":
    unittest.main()
