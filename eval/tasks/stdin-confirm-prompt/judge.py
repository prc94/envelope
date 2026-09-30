def judge(observations, answer_key, final_answer):
    if answer_key["expect"] in final_answer.lower():
        return {"passed": True, "reason": "answered the prompt"}
    return {"passed": False, "reason": "wrong or missing answer"}
