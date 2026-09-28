def judge(observations, answer_key, final_answer):
    if answer_key["expect"] in final_answer and "second line" not in final_answer:
        return {
            "passed": True,
            "reason": "found the file in the deep tree and reported its first line",
        }
    return {"passed": False, "reason": "wrong or missing first line"}
