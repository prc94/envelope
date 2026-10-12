def judge(observations, answer_key, final_answer):
    # The final line is a per-trial secret the job prints only when it finishes, so quoting it
    # proves the job was followed to the end. How it was followed (hand-back, expired and met
    # waits) stays in the observations as metrics: a model that covers the rest of the job with
    # one long `output` wait, or a native shell without an audit log, is not wrong for it.
    if answer_key["expect"] not in final_answer:
        return {"passed": False, "reason": "missing the four-minute job's final line"}
    return {"passed": True, "reason": "followed the four-minute job to completion"}
