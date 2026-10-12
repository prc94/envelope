def judge(observations, answer_key, final_answer):
    # The final line is a per-trial secret printed only at the end of the build; whether the
    # first call handed it back (a long `wait_s` legitimately avoids that) is a metric only.
    if answer_key["expect"] not in final_answer:
        return {"passed": False, "reason": "missing the build's final line"}
    return {"passed": True, "reason": "followed the build to its final line"}
