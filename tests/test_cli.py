"""CLI management command tests for Run/Thread/Checkpoint queries and resume."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main as cli


class CliManagementCommandTests(unittest.TestCase):
    def test_location_still_parses_without_management_flags(self) -> None:
        with patch.object(sys, "argv", ["main.py", "合肥"]):
            arguments = cli.parse_arguments()

        self.assertEqual(arguments.location, "合肥")
        self.assertFalse(arguments.list_runs)
        self.assertIsNone(arguments.resume)

    def test_management_flags_are_parsed(self) -> None:
        with patch.object(
            sys,
            "argv",
            ["main.py", "--list-runs", "--status", "interrupted", "--limit", "5"],
        ):
            arguments = cli.parse_arguments()

        self.assertTrue(arguments.list_runs)
        self.assertEqual(arguments.status, "interrupted")
        self.assertEqual(arguments.limit, 5)

    def test_list_runs_on_empty_database_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "runs.sqlite3")
            with patch.dict("os.environ", {"AGENT_CHECKPOINT_DB_PATH": db_path}):
                with patch.object(sys, "argv", ["main.py", "--list-runs"]):
                    self.assertEqual(cli.main(), 0)

    def test_list_threads_on_empty_database_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "runs.sqlite3")
            with patch.dict("os.environ", {"AGENT_CHECKPOINT_DB_PATH": db_path}):
                with patch.object(sys, "argv", ["main.py", "--list-threads"]):
                    self.assertEqual(cli.main(), 0)

    def test_list_checkpoints_of_unknown_thread_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "runs.sqlite3")
            with patch.dict("os.environ", {"AGENT_CHECKPOINT_DB_PATH": db_path}):
                with patch.object(
                    sys,
                    "argv",
                    ["main.py", "--list-checkpoints", "missing-thread"],
                ):
                    self.assertEqual(cli.main(), 0)

    def test_resume_with_empty_thread_id_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "runs.sqlite3")
            with patch.dict("os.environ", {"AGENT_CHECKPOINT_DB_PATH": db_path}):
                with patch.object(sys, "argv", ["main.py", "--resume", ""]):
                    self.assertEqual(cli.main(), 1)

    def test_resume_of_unknown_thread_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "runs.sqlite3")
            with patch.dict("os.environ", {"AGENT_CHECKPOINT_DB_PATH": db_path}):
                with patch.object(
                    sys,
                    "argv",
                    ["main.py", "--resume", "missing-thread"],
                ):
                    self.assertEqual(cli.main(), 1)


if __name__ == "__main__":
    unittest.main()
