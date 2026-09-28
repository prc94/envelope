def judge(observations, answer_key, final_answer):
    named = {c for c in answer_key["classes"] if c in final_answer}
    if named == {answer_key["expect"]}:
        return {"passed": True, "reason": "named the last traceback's exception class"}
    return {"passed": False, "reason": f"named {sorted(named) or 'no exception class'}"}
