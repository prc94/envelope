def judge(observations, answer_key, final_answer):
    got = sorted(observations["files"])
    if got == answer_key["expect"]:
        return {"passed": True, "reason": "exactly the first file of each duplicate group remains"}
    wrong = sorted(set(got) ^ set(answer_key["expect"]))[:3]
    return {"passed": False, "reason": f"the remaining files differ, for example {wrong}"}
