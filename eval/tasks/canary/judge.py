def judge(observations, answer_key, final_answer):
    return {"passed": observations.get("ok") is True and "canary" in final_answer}
