def classify_case(scenario, responses, effects, observations=None):
    observations = observations or {}
    if len(responses) != scenario.request_count:
        return "error", "Request campaign did not finish"
    if scenario.fault and not observations.get("fault_injected"):
        return "error", "Configured fault was not exercised"
    if len(effects) > 1:
        return "violation", None
    if scenario.fault:
        expected_failure = 500 if scenario.fault == "failure_before_save" else 502
        if (not responses or responses[0]["status_code"] not in {expected_failure, 200}
                or any(reply["status_code"] not in {200, 409} for reply in responses[1:])):
            return "error", "Responses did not match the single-fault retry schedule"
        if (len(effects) == 1 and any(reply["status_code"] == 200 for reply in responses)
                and observations.get("payment_state") == "completed"):
            return "no_violation_reproduced", None
        return "inconclusive", "Retries did not establish durable payment completion; absence of duplicate effects does not establish recovery"
    allowed = {200, 409} if scenario.concurrent else {200}
    if (len(effects) != 1 or not any(reply["status_code"] == 200 for reply in responses)
            or any(reply["status_code"] not in allowed for reply in responses)):
        return "error", "Campaign did not complete a valid payment"
    return "no_violation_reproduced", None
