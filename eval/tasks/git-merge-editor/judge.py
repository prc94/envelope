def judge(observations, answer_key, final_answer):
    o = observations
    if o["merge_in_progress"]:
        return {"passed": False, "reason": "a merge is still in progress"}
    if o["branch"] != "main":
        return {"passed": False, "reason": f"the repository is on {o['branch']}, not main"}
    if o["parents"] != 2 or not o["has_feature_file"]:
        return {"passed": False, "reason": "main has no merge commit that brings in feature"}
    if not o["clean"]:
        return {"passed": False, "reason": "the working tree is not clean"}
    return {"passed": True, "reason": "feature is merged into main with a merge commit"}
