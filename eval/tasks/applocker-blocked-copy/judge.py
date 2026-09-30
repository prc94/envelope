def judge(observations, answer_key, final_answer):
    if answer_key["expect"] in final_answer:
        return {"passed": True, "reason": "reported the blocked exit code"}
    return {"passed": False, "reason": "wrong or missing exit code"}
