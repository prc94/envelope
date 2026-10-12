def judge(observations, answer_key, final_answer):
    # The outcome is the owner's identity, served over HTTP (hits.log is written by the owner on
    # each /identity request). Whether envelope's port-in-use hint was seen is a metric only: a
    # native shell has no audit log, and an agent may find the owner without the hint.
    if not observations["identity"] or final_answer.strip() != observations["identity"]:
        return {"passed": False, "reason": "did not return the owning server's identity"}
    return {"passed": True, "reason": "found and probed the port owner"}
