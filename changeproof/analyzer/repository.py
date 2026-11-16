import os
from pathlib import Path
import tokenize

from .ast_parser import parse_source
from .diff_analyzer import AnalysisError, changed_lines, touches
from .effect_detector import candidates

EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", "reports"}


def analyze_repository(repo, config, *, base=None):
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise AnalysisError("Repository path must be an existing directory")
    root, revision, changes = changed_lines(repo, base) if base else (repo, None, None)
    files = []
    for directory, dirs, names in os.walk(repo, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in EXCLUDED
                         and not (Path(directory) / name).is_symlink())
        for name in sorted(names):
            path = Path(directory) / name
            if path.suffix == ".py" and not path.is_symlink():
                files.append(path)
    if len(files) > 1000:
        raise AnalysisError("Analysis is limited to 1,000 Python files; choose a smaller directory")
    functions, context, diagnostics = [], [], []
    for path in files:
        relative = path.relative_to(root).as_posix()
        if changes is not None and relative not in changes:
            continue
        try:
            if path.stat().st_size > 1_000_000:
                raise ValueError("Python file exceeds the 1 MB analysis limit")
            with tokenize.open(path) as source:
                parsed = parse_source(source.read(), relative)
            for function in parsed:
                function["changed"] = None if changes is None else touches(function, changes[relative])
                function["matches_endpoint"] = any(route["method"] == config.service.method and
                    route["path"] == config.service.endpoint for route in function["routes"])
                context.append(function)
                if changes is None or function["changed"]:
                    functions.append(function)
        except (SyntaxError, UnicodeError, OSError, ValueError, RecursionError) as exc:
            diagnostics.append({"path": relative, "error": type(exc).__name__, "message": str(exc)})
    edges = local_call_graph(functions, context)
    findings = []
    for function in functions:
        for candidate in candidates(function):
            findings.append({"path": function["path"], "function": function["qualified_name"], **candidate})
    return {"schema_version": 1, "status": "incomplete" if diagnostics else "analysis_complete",
            "repo": str(repo), "base_commit": revision,
            "configured_endpoint": {"method": config.service.method, "path": config.service.endpoint},
            "functions": functions, "call_graph": edges,
            "endpoint_functions": [{"path": item["path"], "function": item["qualified_name"]}
                                   for item in context if item["matches_endpoint"]],
            "candidates": findings, "diagnostics": diagnostics,
            "limitations": ["Static candidates are unconfirmed hypotheses; no target code was executed.",
                "Call-name and literal-SQL heuristics; no alias, type, data-flow, or path feasibility proof.",
                "Claims are recorded but do not establish atomicity or suppress candidates.",
                "Call graph contains lexical and self/cls call matches within a file, not runtime dispatch.",
                "Only direct literal route decorators are recognized; router prefixes are not resolved.",
                "Git mode examines current functions overlapping changes, not deleted functions."]}


def local_call_graph(functions, context):
    index = {}
    for item in context:
        index.setdefault((item["path"], item["qualified_name"]), []).append(item)
    edges = []
    for caller in functions:
        scope = caller["qualified_name"].split(".")[:-1]
        for call in caller["calls"]:
            name = call["name"]
            options = []
            if name.startswith(("self.", "cls.")):
                options = [".".join([*scope, name.split(".", 1)[1]])]
            elif "." not in name:
                # Try nested lexical scopes before module scope.
                options = [".".join([*scope[:depth], name]) for depth in range(len(scope), -1, -1)]
                options.insert(0, caller["qualified_name"] + "." + name)
            for option in options:
                matches = index.get((caller["path"], option), [])
                if len(matches) == 1:
                    edges.append({"path": caller["path"], "caller": caller["qualified_name"],
                                  "callee": option, "line": call["line"], "status": "syntactic"})
                    break
    return edges
