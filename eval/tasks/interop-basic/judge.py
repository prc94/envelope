def judge(observations, answer_key, final_answer):
    if answer_key["expect"] not in final_answer:
        return {"passed": False, "reason": "the reply does not contain the command's output"}
    if not any(run["exit_code"] == 0 for run in observations["runs"]):
        return {"passed": False, "reason": "the answer was not obtained by running the command"}
    return {"passed": True, "reason": "correct output from a successful run"}
