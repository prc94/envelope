def judge(observations, answer_key, final_answer):
    if answer_key["expect"] in final_answer:
        return {"passed": True, "reason": "correct body"}
    return {"passed": False, "reason": "wrong or missing body"}
