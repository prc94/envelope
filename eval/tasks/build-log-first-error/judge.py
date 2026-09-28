def judge(observations, answer_key, final_answer):
    if answer_key["expect"] in final_answer.replace("`", " ").split():
        return {"passed": True, "reason": "named the first failing module"}
    return {"passed": False, "reason": "wrong or missing module name"}
