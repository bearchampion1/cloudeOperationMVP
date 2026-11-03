"""LLM 履歷評分，作法參考課程講義（LM Studio + qwen3-4b）：

1. 決策呼叫：HR 在後台設定「能力標籤 + 權重」，模型只輸出固定格式的純文字——每個能力一行分數，再加一行 HIGHLIGHTS
2. 用字串解析 + 白名單驗證；格式不對就把錯誤當成 Observation 回饋給模型重試，最多 MAX_STEPS 次
3. 總分由程式依權重計算（不讓模型自己算加權），建議（推薦／待定／不推薦）也由分數門檻決定
4. 最後答案呼叫：另外請模型寫給 HR 的摘要，明確要求「只能根據履歷內容，不得編造」

temperature 可在後台調整：硬技能看履歷有沒有寫，軟技能允許模型從自傳與經歷合理推論，所以預設不是 0。
只用標準函式庫（urllib）呼叫 LM Studio 的 OpenAI 相容 API，不需要安裝 openai 套件。
"""
import json
import re
import urllib.error
import urllib.request

MAX_STEPS = 3
VERDICTS = ["推薦", "待定", "不推薦"]
PASS_SCORE = 75       # 總分 >= 75 推薦
MAYBE_SCORE = 50      # 總分 >= 50 待定，以下不推薦
REQUIRED_MIN = 50     # 「必備」項目低於此分數時，最多只給「待定」
DEFAULT_TEMPERATURE = 0.4


class LLMError(Exception):
    pass


def chat(cfg, messages, timeout=120):
    """呼叫 POST {base_url}/chat/completions，回傳文字內容。"""
    url = cfg["base_url"].rstrip("/") + "/chat/completions"
    temperature = float(cfg.get("temperature", DEFAULT_TEMPERATURE))
    body = json.dumps({"model": cfg["model"], "messages": messages, "temperature": temperature}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.get('api_key') or 'lm-studio'}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read().decode())
    except urllib.error.HTTPError as e:
        raise LLMError(f"LLM 伺服器回傳錯誤 {e.code}：{e.read().decode(errors='replace')[:200]}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise LLMError(f"連不到 LLM 伺服器（{url}），請確認 LM Studio 已啟動 Local Server：{e}")
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError("LLM 回應格式不符合 OpenAI 相容格式")
    # 有些模型會先輸出 <think>…</think>，去掉只留答案
    return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()


def list_models(cfg, timeout=10):
    url = cfg["base_url"].rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cfg.get('api_key') or 'lm-studio'}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        raise LLMError(f"連不到 LLM 伺服器（{url}）：{e}")
    return [m.get("id") for m in data.get("data", [])]


def format_resume(fields, data):
    lines = []
    for f in fields:
        v = data.get(f["key"], "")
        if isinstance(v, list):
            v = "、".join(v)
        elif isinstance(v, dict):  # 附件：伺服器沒換成文字時只給檔名
            v = v.get("name", "")
        lines.append(f"{f['label']}：{str(v).strip() or '（未填）'}")
    return "\n".join(lines)


def build_messages(cfg, resume_text):
    criteria = cfg["criteria"]
    n = len(criteria)
    total = sum(c["weight"] for c in criteria)
    items = "\n".join(
        f"{i}. {c['name']}（權重 {round(c['weight'] * 100 / total)}%{'，必備' if c.get('required') else ''}）"
        + (f"：{c['desc']}" if c.get("desc") else "")
        for i, c in enumerate(criteria, 1)
    )
    lines = "\n".join(f"{i}:0到100的整數" for i in range(1, n + 1))
    example = "\n".join(f"{i}:{[80, 45, 65, 90, 30][(i - 1) % 5]}" for i in range(1, n + 1))
    system = (
        "你是一個履歷篩選助理，負責依照 HR 列出的評估項目，替應徵者每一項打 0 到 100 分。\n"
        "評分原則：\n"
        "1. 硬技能（程式語言、工具、證照、學歷、年資）以履歷明確寫到的為準，完全沒提到給 0 到 30 分。\n"
        "2. 軟技能（溝通、團隊合作、領導、學習能力、抗壓、主動性等）不必要求出現一模一樣的字眼，"
        "可以從自傳、經歷、專案描述合理推論；有相關跡象就給部分分數，跡象越具體分數越高。\n"
        "3. 不可以編造履歷沒有的經歷。\n"
        f"只能輸出以下 {n + 1} 行，不要加入其他文字：\n{lines}\n"
        "HIGHLIGHTS:履歷中最突出的 3 到 6 個技能或亮點，用「、」分隔\n"
        f"例如：\n{example}\nHIGHLIGHTS:Python、帶領 3 人專案、自學機器學習"
    )
    user = (f"職缺說明：\n{cfg.get('requirements') or '（未提供）'}\n\n"
            f"評估項目：\n{items}\n\n應徵者履歷：\n{resume_text}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_decision(raw, n):
    """把模型輸出解析成 (各項分數 list, 亮點 list)，格式不對就丟 ValueError。"""
    values = {}
    for line in raw.splitlines():
        line = line.strip().lstrip("-*• ")
        if re.search(r"[:：]", line):
            key, value = re.split(r"[:：]", line, maxsplit=1)
            values[key.strip().rstrip(".、").upper()] = value.strip()
    scores = []
    for i in range(1, n + 1):
        if str(i) not in values:
            raise ValueError(f"缺少第 {i} 項的分數")
        m = re.fullmatch(r"(\d{1,3})\s*分?", values[str(i)])
        if not m:
            raise ValueError(f"第 {i} 項的分數不是整數：{values[str(i)]}")
        score = int(m.group(1))
        if not 0 <= score <= 100:
            raise ValueError(f"第 {i} 項的分數超出 0～100：{score}")
        scores.append(score)
    highlights = []
    for h in re.split(r"[、,，;；/]", values.get("HIGHLIGHTS", "")):
        h = h.strip(" 「」\"'")[:30]
        if h and h not in highlights:
            highlights.append(h)
    if not highlights:
        raise ValueError("缺少 HIGHLIGHTS")
    return scores, highlights[:8]


def overall(criteria, scores):
    """依權重算總分，再依門檻決定建議。"""
    total = sum(c["weight"] for c in criteria)
    score = round(sum(c["weight"] * s for c, s in zip(criteria, scores)) / total)
    verdict = "推薦" if score >= PASS_SCORE else "待定" if score >= MAYBE_SCORE else "不推薦"
    if verdict == "推薦" and any(c.get("required") and s < REQUIRED_MIN for c, s in zip(criteria, scores)):
        verdict = "待定"
    return score, verdict


def generate_summary(cfg, messages, decision):
    final = messages + [
        {"role": "assistant", "content": decision},
        # 用 user 角色下指令：部分模型的對話模板不接受出現在中間的 system 訊息
        {"role": "user", "content": (
            "你現在要寫給 HR 看的摘要。\n"
            "規則：\n"
            "1. 只能根據履歷內容與評估項目，不可以編造履歷沒有的資訊。\n"
            "2. 使用繁體中文。\n"
            "3. 第一句總結這位應徵者的定位；接著列出主要優勢；最後列出不足或需要面試確認的地方。"
            "軟技能如果是推論出來的，請說明是從履歷哪裡看出來的。\n"
            "4. 總長度不超過 200 字。\n"
            "5. 不要輸出分數或 HIGHLIGHTS。"
        )},
    ]
    return chat(cfg, final)


def screen_resume(cfg, fields, data):
    """回傳 {"score", "verdict", "summary", "highlights", "breakdown", "steps"}，失敗丟 LLMError。"""
    criteria = cfg.get("criteria") or []
    if not criteria:
        raise LLMError("請先在「AI 篩選」頁新增至少一個評估項目")
    messages = build_messages(cfg, format_resume(fields, data))
    for step in range(1, MAX_STEPS + 1):
        raw = chat(cfg, messages)
        try:
            scores, highlights = parse_decision(raw, len(criteria))
        except ValueError as e:
            # 驗證失敗：把錯誤回饋給模型，請它重新輸出
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": (
                    f"Observation: 上一個輸出未通過格式驗證（{e}）。\n"
                    f"只能輸出 {len(criteria) + 1} 行：\n"
                    + "\n".join(f"{i}:0到100的整數" for i in range(1, len(criteria) + 1))
                    + "\nHIGHLIGHTS:用「、」分隔的亮點\n不要輸出其他文字。"
                )},
            ]
            continue
        score, verdict = overall(criteria, scores)
        decision = "\n".join(f"{i}:{s}" for i, s in enumerate(scores, 1)) + "\nHIGHLIGHTS:" + "、".join(highlights)
        summary = generate_summary(cfg, messages, decision)
        breakdown = [{"name": c["name"], "weight": c["weight"], "required": bool(c.get("required")), "score": s}
                     for c, s in zip(criteria, scores)]
        return {"score": score, "verdict": verdict, "summary": summary, "highlights": highlights,
                "breakdown": breakdown, "steps": step}
    raise LLMError(f"模型連續 {MAX_STEPS} 次沒有輸出正確格式，請換一個模型或減少評估項目")
