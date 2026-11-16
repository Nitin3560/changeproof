import contextlib
import copy
import io
from pathlib import Path
import tempfile
import unittest

import yaml
from pydantic import ValidationError

from changeproof.cli import main
from changeproof.invariants.loader import ConfigurationError, load_config
from changeproof.invariants.models import CampaignConfig

EXAMPLE = Path(__file__).resolve().parents[1] / "configs/payment-idempotency.yaml"


class ConfigurationTests(unittest.TestCase):
    def test_example(self):
        config = load_config(EXAMPLE)
        self.assertEqual(config.invariants[0].max_occurrences, 1)
        self.assertEqual(config.scenarios.concurrent_requests, 10)

    def test_invalid_configuration(self):
        original = yaml.safe_load(EXAMPLE.read_text())
        variants = []
        for field, value in [("concurrent_requests", 1), ("concurrent_requests", 101),
                             ("iterations", 0), ("iterations", 1001),
                             ("iterations", "100"), ("concurrent_duplicates", "true")]:
            data = copy.deepcopy(original)
            data["scenarios"][field] = value
            variants.append(data)
        data = copy.deepcopy(original)
        data["invariants"].append(copy.deepcopy(data["invariants"][0]))
        variants.append(data)
        data = copy.deepcopy(original)
        data["scenarios"].update(sequential_duplicates=False, concurrent_duplicates=False)
        variants.append(data)
        for field, value in [("type", "unknown"), ("max_occurrences", 2), ("effect", "rows")]:
            data = copy.deepcopy(original)
            data["invariants"][0][field] = value
            variants.append(data)
        data = copy.deepcopy(original)
        data["unexpected"] = True
        variants.extend([data, {}, None])
        for data in variants:
            with self.subTest(data=data), self.assertRaises(ValidationError):
                CampaignConfig.model_validate(data)

    def test_yaml_and_file_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            for content in ["service: 1\nservice: 2", "[", "", "!!python/object:foo {}"]:
                path.write_text(content)
                with self.subTest(content=content), self.assertRaises(ConfigurationError):
                    load_config(path)
            with self.assertRaises(ConfigurationError):
                load_config(Path(directory) / "missing.yaml")

    def test_cli(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["validate", "--config", str(EXAMPLE)]), 0)
        self.assertIn('"status": "configuration_valid"', out.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(main(["validate", "--config", "/nonexistent/changeproof.yaml"]), 2)
        self.assertIn("Invalid configuration", err.getvalue())
