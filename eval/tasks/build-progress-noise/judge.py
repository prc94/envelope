def judge(observations, answer_key, final_answer):
    if answer_key["expect"] in final_answer:
        return {"passed": True, "reason": "reported the artifact id the script prints"}
    return {"passed": False, "reason": "wrong or missing artifact id"}
