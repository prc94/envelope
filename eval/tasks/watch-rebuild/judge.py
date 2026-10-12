def judge(observations, answer_key, final_answer):
    # The watcher hashes the file's raw bytes, so `version=2` written without a trailing newline,
    # or with CRLF, is as correct as `version=2\n`: the expected result is the hash of whatever
    # the file holds after the edit. The number of `output` reads is a metric only.
    if observations["source"].strip() != "version=2":
        return {"passed": False, "reason": "source was not edited"}
    if observations["result"] not in final_answer:
        return {"passed": False, "reason": "did not follow the new watcher result"}
    return {"passed": True, "reason": "followed the watcher after an edit"}
