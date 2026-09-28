def judge(observations, answer_key, final_answer):
    want = answer_key["expect"]
    if observations["inbox"]:
        return {"passed": False, "reason": f"inbox still holds {len(observations['inbox'])} files"}
    for kind in ("spaced", "plain"):
        got = sorted(observations[kind])
        if got != want[kind]:
            missing = sorted(set(want[kind]) - set(got))[:2]
            extra = sorted(set(got) - set(want[kind]))[:2]
            return {
                "passed": False,
                "reason": f"sorted/{kind} differs: missing {missing}, unexpected {extra}",
            }
    return {"passed": True, "reason": "every file is in the right directory"}
