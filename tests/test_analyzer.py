import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import textwrap
from unittest import TestCase

from changeproof.analyzer.ast_parser import parse_source
from changeproof.analyzer.diff_analyzer import AnalysisError
from changeproof.analyzer.effect_detector import candidates
from changeproof.analyzer.repository import analyze_repository
from changeproof.cli import main
from changeproof.invariants.loader import load_config

CONFIG = Path(__file__).resolve().parents[1] / "configs/payment-idempotency.yaml"
VULNERABLE = textwrap.dedent("""\
@app.post("/payments")
async def payment(key, amount):
    existing = await db.get_payment(key)
    if existing:
        return existing
    charge = await gateway.charge(amount)
    await db.save_payment(key, charge)
    return charge
""")


class ParserTests(TestCase):
    def test_endpoint_and_candidate_locations(self):
        function = parse_source(VULNERABLE, "service.py")[0]
        self.assertEqual(function["qualified_name"], "payment")
        self.assertEqual(function["start_line"], 1)
        self.assertEqual(function["routes"], [{"method": "POST", "path": "/payments", "line": 1}])
        finding = candidates(function)[0]
        self.assertEqual(finding["status"], "candidate")
        self.assertEqual(finding["read"]["line"], 3)
        self.assertEqual(finding["side_effect"]["line"], 6)
        self.assertEqual(finding["write"]["line"], 7)
        self.assertEqual(function["assignments"][0]["targets"], ["existing"])

    def test_nested_scopes_do_not_mix_effects(self):
        source = textwrap.dedent("""\
        def outer():
            async def inner():
                return await gateway.charge(5)
            existing = db.get_payment("key")
            if existing:
                return existing
            db.save_payment("key")
            return inner
        """)
        outer, inner = parse_source(source)
        self.assertEqual(inner["qualified_name"], "outer.inner")
        self.assertEqual(candidates(outer), [])
        self.assertEqual(candidates(inner), [])
        self.assertFalse(any(call["name"] == "gateway.charge" for call in outer["calls"]))

    def test_claim_is_context_not_proof(self):
        source = VULNERABLE.replace("    charge =", "    await store.claim(key)\n    charge =")
        finding = candidates(parse_source(source)[0])[0]
        self.assertEqual(finding["observed_claims"][0]["name"], "store.claim")
        self.assertEqual(finding["status"], "candidate")

    def test_incomplete_patterns_do_not_match(self):
        for source in (VULNERABLE.replace("    await db.save_payment(key, charge)\n", ""),
                       VULNERABLE.replace("    if existing:\n        return existing\n", ""),
                       VULNERABLE.replace("gateway.charge", "gateway.quote")):
            self.assertEqual(candidates(parse_source(source)[0]), [])

    def test_sql_http_and_exception_facts(self):
        source = textwrap.dedent('''\
        async def pay():
            row = await conn.execute("SELECT * FROM payments WHERE key = 'x'")
            if row:
                return row
            try:
                await client.post("/charges", json={})
            except TimeoutError:
                raise
            await conn.execute("UPDATE payments SET amount = 10")
        ''')
        function = parse_source(source)[0]
        self.assertEqual(len(candidates(function)), 1)
        self.assertEqual(function["exceptions"][0]["type"], "TimeoutError")

    def test_nested_call_evaluation_order(self):
        function = parse_source("def pay():\n    db.save_payment(gateway.charge(1))\n")[0]
        self.assertEqual([call["kind"] for call in function["calls"]], ["side_effect", "write"])


class RepositoryTests(TestCase):
    def setUp(self):
        self.config = load_config(CONFIG)

    def test_source_is_not_executed_and_errors_are_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            marker = root / "executed"
            (root / "app.py").write_text(f"open({str(marker)!r}, 'w').write('bad')\n" + VULNERABLE)
            (root / "broken.py").write_text("def broken(:")
            ignored = root / ".venv"
            ignored.mkdir()
            (ignored / "noise.py").write_text("def invalid(:")
            report = analyze_repository(root, self.config)
            self.assertFalse(marker.exists())
            self.assertEqual(report["status"], "incomplete")
            self.assertEqual(len(report["diagnostics"]), 1)
            self.assertEqual(len(report["candidates"]), 1)
            self.assertTrue(report["functions"][0]["matches_endpoint"])

    def test_lexical_and_method_call_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "service.py").write_text(textwrap.dedent("""\
            def helper():
                return 1
            @app.post("/payments")
            def endpoint():
                return helper()
            class Service:
                def pay(self):
                    return self.save()
                def save(self):
                    return 1
            """))
            report = analyze_repository(root, self.config)
            edges = {(edge["caller"], edge["callee"]) for edge in report["call_graph"]}
            self.assertIn(("endpoint", "helper"), edges)
            self.assertIn(("Service.pay", "Service.save"), edges)
            self.assertEqual(report["endpoint_functions"], [{"path": "service.py", "function": "endpoint"}])

    def test_cli_json_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "app.py").write_text(VULNERABLE)
            output = root / "analysis.json"
            args = ["analyze", "--repo", str(root), "--config", str(CONFIG)]
            self.assertEqual(main(args + ["--output", str(output)]), 0)
            report = json.loads(output.read_text())
            self.assertEqual(report["candidates"][0]["status"], "candidate")
            # A candidate is successful analysis, not a reproduced-verification exit code.
            (root / "bad.py").write_text("def bad(:")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args), 2)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(args + ["--base", "missing-revision"]), 2)

    def test_git_changes_include_staged_unstaged_and_untracked(self):
        with tempfile.TemporaryDirectory(prefix="changeproof git ") as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.run(["git", "-C", str(root), *args], check=True,
                                      capture_output=True, text=True).stdout.strip()
            git("init")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            path = root / "payment service.py"
            path.write_text(VULNERABLE + "\ndef unchanged():\n    return 1\n")
            git("add", ".")
            git("commit", "-m", "Initial service")
            base = git("rev-parse", "HEAD")
            self.assertEqual(analyze_repository(root, self.config, base=base)["functions"], [])
            path.write_text(path.read_text().replace("gateway.charge(amount)", "gateway.charge(amount + 1)"))
            git("add", ".")
            (root / "new.py").write_text("def added():\n    return 1\n")
            report = analyze_repository(root, self.config, base=base)
            self.assertEqual(report["base_commit"], base)
            self.assertEqual({f["name"] for f in report["functions"]}, {"payment", "added"})
            path.write_text(path.read_text().replace("return 1", "return 2"))
            report = analyze_repository(root, self.config, base=base)
            self.assertEqual({f["name"] for f in report["functions"]}, {"payment", "unchanged", "added"})
            with self.assertRaises(AnalysisError):
                analyze_repository(root, self.config, base="--help")
