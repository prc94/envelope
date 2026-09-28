def judge(observations, answer_key, final_answer):
    o = observations
    if o["merge_in_progress"] or o["conflict_markers"]:
        return {"passed": False, "reason": "the merge is unfinished or conflict markers remain"}
    if o["branch"] != "main" or o["parents"] != 2:
        return {"passed": False, "reason": "main does not end in a merge commit"}
    if o["timeout"] != answer_key["expect"]:
        return {
            "passed": False,
            "reason": f"timeout is {o['timeout']}, expected the larger value {answer_key['expect']}",
        }
    return {"passed": True, "reason": "merged with the larger timeout"}
