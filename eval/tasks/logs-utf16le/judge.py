import re


def judge(observations, answer_key, final_answer):
    numbers = set(re.findall(r"\d+", final_answer))
    if numbers == {str(answer_key["expect"])}:
        return {"passed": True, "reason": "correct count"}
    return {
        "passed": False,
        "reason": f"expected {answer_key['expect']}, reply had {sorted(numbers)}",
    }
