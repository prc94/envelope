def judge(observations, answer_key, final_answer):
    if answer_key["expect"] in final_answer:
        return {"passed": True, "reason": "correct author email"}
    return {"passed": False, "reason": "the reply does not contain the author's email"}
