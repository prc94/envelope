def judge(observations, answer_key, final_answer):
    # The server reads config.txt once at start-up, so a `version=1` request followed by a
    # `version=2` one proves it was restarted after the edit, whether through envelope's
    # `restart` parameter, a stop and a fresh start, or a native shell; `restarts` is a metric.
    if observations["config"].strip() != "version=2":
        return {"passed": False, "reason": "config.txt was not changed to version=2"}
    if observations["hits"][:1] != ["version=1"] or "version=2" not in observations["hits"][1:]:
        return {"passed": False, "reason": "server was not restarted after the edit"}
    if final_answer.strip() != answer_key["expect"]:
        return {"passed": False, "reason": "incorrect new server value"}
    return {"passed": True, "reason": "restarted and probed the edited server"}
