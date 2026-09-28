def judge(observations, answer_key, final_answer):
    if answer_key["expect"] not in final_answer:
        return {"passed": False, "reason": "wrong or missing TOKEN value"}
    if not any(run["exit_code"] == 0 for run in observations["runs"]):
        return {"passed": False, "reason": "the command was never run successfully"}
    if observations["outputs_ok"] < 1:
        return {"passed": False, "reason": "the omitted output was not retrieved with output"}
    return {"passed": True, "reason": "found the token through the output tool"}
