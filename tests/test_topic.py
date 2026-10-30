"""Tests for topic validation in the CLI-independent core."""

import subprocess
import sys
import unittest

from app.core.topic import MAX_TOPIC_LENGTH, validate_topic


class ValidateTopicTests(unittest.TestCase):
    def test_strips_outer_whitespace(self) -> None:
        self.assertEqual(validate_topic("  climate adaptation  "), "climate adaptation")

    def test_rejects_blank_and_whitespace_only_values(self) -> None:
        for value in ("", " \t\n "):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "blank"):
                validate_topic(value)

    def test_accepts_topic_at_maximum_length(self) -> None:
        topic = "x" * MAX_TOPIC_LENGTH
        self.assertEqual(validate_topic(topic), topic)

    def test_rejects_topic_over_maximum_length(self) -> None:
        with self.assertRaisesRegex(ValueError, str(MAX_TOPIC_LENGTH)):
            validate_topic("x" * (MAX_TOPIC_LENGTH + 1))

    def test_rejects_non_string_input(self) -> None:
        with self.assertRaisesRegex(ValueError, "text"):
            validate_topic(None)  # type: ignore[arg-type]

    def test_core_import_does_not_load_cli(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import app.core.topic, sys; "
                "assert 'app.cli.main' not in sys.modules",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
