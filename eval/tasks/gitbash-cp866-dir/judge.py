def judge(observations, answer_key, final_answer):
    if answer_key["expect"] in final_answer:
        return {
            "passed": True,
            "reason": "found the Cyrillic file name (recovering from the native listing if it was garbled)",
        }
    return {"passed": False, "reason": "the file name is missing or garbled in the reply"}
