import ast
import re

READS = {"payment", "get_payment", "find_payment", "lookup_payment", "get_transaction",
         "find_transaction", "fetchone", "fetchrow"}
CHARGES = {"charge", "create_charge", "charge_payment", "process_charge"}
WRITES = {"save_payment", "save_transaction", "record_payment", "insert_payment"}
CLAIMS = {"claim", "claim_payment", "reserve_payment", "reserve_key", "claim_key"}


def dotted_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = dotted_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def classify_call(node):
    name = dotted_name(node.func)
    leaf = name.rsplit(".", 1)[-1]
    literal = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else None
    if leaf == "execute" and isinstance(literal, str):
        query = " ".join(literal.lower().split())
        if re.search(r"\b(select|insert|update)\b", query) and "payments" in query:
            if query.startswith("select"):
                return "read"
            if query.startswith("insert") and "on conflict" in query:
                return "claim"
            if query.startswith(("insert", "update")):
                return "write"
    if leaf in CHARGES or (leaf == "post" and literal == "/charges"):
        return "side_effect"
    if leaf in CLAIMS:
        return "claim"
    if leaf in READS:
        return "read"
    if leaf in WRITES or (leaf == "save" and name.split(".")[0] in {"db", "store", "repo"}):
        return "write"
    return "other"


def candidates(function):
    calls = function["calls"]
    for read_index, read in enumerate(calls):
        if read["kind"] != "read":
            continue
        for effect_index in range(read_index + 1, len(calls)):
            effect = calls[effect_index]
            if effect["kind"] != "side_effect":
                continue
            branches = [item for item in function["branches"]
                        if read["line"] <= item["line"] <= effect["line"]]
            write = next((item for item in calls[effect_index + 1:] if item["kind"] == "write"), None)
            if not branches or write is None:
                continue
            claims = [item for item in calls[:effect_index] if item["kind"] == "claim"]
            return [{"status": "candidate", "hypothesis": "non_atomic_idempotency_check",
                     "risk": "duplicate_external_charge", "read": read,
                     "branch": branches[0], "side_effect": effect, "write": write,
                     "observed_claims": claims,
                     "reason": "Read, branch, external charge, and write occur in this function; "
                               "atomicity and reachable paths have not been established.",
                     "recommended_scenario": {"type": "concurrent_duplicates", "request_count": 2,
                                               "synchronization_point": "after_key_lookup"}}]
    return []
