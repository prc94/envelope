def judge(observations, answer_key, final_answer):
    if answer_key["expect"].lower() in final_answer.lower():
        return {"passed": True, "reason": "correct hash"}
    return {"passed": False, "reason": "wrong or missing hash"}
