import ast
import json
from pathlib import Path
import tempfile
from unittest import TestCase

from changeproof.execution.runner import CaseResult
from changeproof.execution.target import TargetError, load_target
from changeproof.invariants.loader import load_config
from changeproof.reporting.reports import write_reports


class ReportTests(TestCase):
    def test_evidence_and_reproduction(self):
        config = load_config(Path(__file__).resolve().parents[1] / "configs/payment-idempotency.yaml")
        config.scenarios.iterations = 1
        cases = [CaseResult("single_payment", "one", "no_violation_reproduced", effects=[{}]),
                 CaseResult("sequential_duplicates", "two", "no_violation_reproduced", effects=[{}]),
                 CaseResult("concurrent_duplicates", "three", "violation",
                            effects=[{"charge_id": "a"}, {"charge_id": "b"}],
                            responses=[{"status_code": 200}, {"status_code": 200}],
                            trace=[{"stage": "before_charge"}])]
        with tempfile.TemporaryDirectory() as directory:
            report, folder = write_reports(directory, config, {"path": "/sample", "variant": "vulnerable"}, cases)
            self.assertEqual(report["status"], "violation_reproduced")
            self.assertTrue(report["campaign"]["complete"])
            finding = json.loads((folder / "CP-001.json").read_text())
            self.assertEqual(finding["observed_charges"], 2)
            self.assertEqual(finding["status"], "reproduced")
            self.assertEqual(finding["trace"], cases[-1].trace)
            ast.parse((folder / "CP-001-repro.py").read_text())
            self.assertTrue((folder / "report.md").is_file())
            report, other = write_reports(directory, config, {"path": "/sample", "variant": "corrected"}, [])
            self.assertEqual(report["status"], "execution_error")
            self.assertNotEqual(folder, other)

    def test_unsupported_target_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(TargetError):
                load_target(directory)
