def judge(observations, answer_key, final_answer):
    if final_answer.strip() != answer_key["expect"]:
        return {"passed": False, "reason": "incorrect final reply"}
    if not observations["handed_back"] or not observations["cleaned"]:
        return {"passed": False, "reason": "the background process was left to session shutdown"}
    return {"passed": True, "reason": "ended the handed-back process"}
