def judge(observations, answer_key, final_answer):
    want = answer_key["expect"]
    if sorted(observations["files"]) != sorted(want):
        return {"passed": False, "reason": "the set of files in legacy changed"}
    for name, text in want.items():
        got = observations["files"][name]
        if not got["utf8"]:
            return {"passed": False, "reason": f"{name} is not valid UTF-8"}
        if got["text"] != text:
            return {"passed": False, "reason": f"{name}: the text or its line endings changed"}
    return {"passed": True, "reason": "every file was converted losslessly"}
