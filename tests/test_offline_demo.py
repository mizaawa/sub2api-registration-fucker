from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest import mock

from offline_demo import main, simulate, write_csv


class OfflineDemoTests(unittest.TestCase):
    def test_default_simulation(self):
        events = simulate()
        self.assertEqual(len(events), 10000)
        self.assertEqual(events[-1].virtual_seconds, 1999.8)
        self.assertTrue(all(event.synthetic and event.http_status == 200 for event in events))

    def test_global_rate(self):
        events = simulate(count=5, rate=4)
        self.assertEqual([event.virtual_seconds for event in events], [0, 0.25, 0.5, 0.75, 1])

    def test_rate_limit_delays_whole_queue(self):
        events = simulate(count=6, rate=5, scenario="rate-limit")
        self.assertEqual(events[2].http_status, 429)
        self.assertEqual(events[3].virtual_seconds - events[2].virtual_seconds, 30)
        self.assertAlmostEqual(events[4].virtual_seconds - events[3].virtual_seconds, 0.2)

    def test_verification_stops_at_third_event(self):
        events = simulate(count=100, scenario="verification")
        self.assertEqual(len(events), 3)
        self.assertEqual(events[-1].outcome, "verification_required")

    def test_short_run_does_not_trigger_special_scenarios(self):
        for scenario in ("rate-limit", "verification"):
            self.assertTrue(all(event.http_status == 200 for event in simulate(2, scenario=scenario)))

    def test_invalid_parameters(self):
        for options in ({"count": 0}, {"count": True}, {"count": 1000001},
                        {"rate": 0}, {"rate": 1.5}, {"rate": 1001}, {"scenario": "invalid"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                simulate(**options)

    def test_csv_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "results" / "demo.csv"
            write_csv(path, simulate(3))
            original = path.read_bytes()
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["synthetic"], "True")
            with self.assertRaises(FileExistsError):
                write_csv(path, simulate(1))
            self.assertEqual(path.read_bytes(), original)

    def test_cli_summary_without_network_or_processes(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network attempted")), \
                mock.patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS attempted")), \
                mock.patch.object(subprocess, "Popen", side_effect=AssertionError("process attempted")), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--count", "10"]), 0)
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["mode"], "offline_synthetic")
        self.assertEqual(summary["completed_events"], 10)

    def test_verification_exit_code(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--scenario", "verification"]), 2)

    def test_invalid_cli_argument(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main(["--rate", "0"])
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
