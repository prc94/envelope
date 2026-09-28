def judge(observations, answer_key, final_answer):
    if answer_key["expect"] not in final_answer:
        return {"passed": False, "reason": "the reply does not contain the command's output"}
    long_runs = [r for r in observations["runs"] if (r["duration_ms"] or 0) >= 39_000]
    if not any(r["exit_code"] == 0 for r in long_runs):
        return {"passed": False, "reason": "no successful run of the full 40 seconds"}
    return {"passed": True, "reason": "waited for the long command and reported its output"}
