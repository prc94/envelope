def judge(observations, answer_key, final_answer):
    if observations["hits"] < 1:
        return {"passed": False, "reason": "the live server was not probed"}
    if final_answer.strip() != observations["value"]:
        return {"passed": False, "reason": "incorrect HTTP response"}
    return {"passed": True, "reason": "probed the live server"}
