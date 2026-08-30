"""ニュース判定。

1. タイトルのキーワードで一次判定（無料・即時）
2. 方向が分からないもの（業績予想の修正 等）は PDF 本文を読んで上方/下方を推定
3. ANTHROPIC_API_KEY があれば PDF 本文を Claude に渡して最終判定（任意）

戻り値: None（スキップ） or dict(labels, score, ambiguous, note, ai)
"""
import io
import json
import os
import re

import requests

import config
from tdnet import HEADERS, Disclosure


def keyword_judge(title: str):
    if any(k in title for k in config.SKIP_KEYWORDS):
        return None
    labels, score = [], 0
    for k, w in config.POSITIVE_KEYWORDS.items():
        if k in title and not any(k in l for l in labels):   # "自己株式の取得" と "自己株式取得" の二重計上を避ける
            labels.append(k)
            score += w
    ambiguous = [k for k in config.AMBIGUOUS_KEYWORDS if k in title]
    if score == 0 and not ambiguous:
        return None
    return {"labels": labels, "score": score, "ambiguous": ambiguous, "note": "", "ai": None}


def fetch_pdf_text(url: str, max_pages: int = 3) -> str:
    if not url:
        return ""
    try:
        from pypdf import PdfReader
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        reader = PdfReader(io.BytesIO(r.content))
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])
        return re.sub(r"[ \t\u3000]+", " ", text)
    except Exception as e:
        print(f"[judge] pdf error {url}: {e}")
        return ""


def pdf_direction(text: str):
    """本文中の語の出現数で上方/下方を推定。判定不能なら None。"""
    if not text:
        return None
    pos = sum(text.count(w) for w in config.PDF_POSITIVE_WORDS)
    neg = sum(text.count(w) for w in config.PDF_NEGATIVE_WORDS)
    if pos == 0 and neg == 0:
        return None
    return "positive" if pos > neg else "negative"


AI_SYSTEM = """あなたは日本株の適時開示を読み、短期の株価インパクトを保守的に判定するアナリストです。
必ず次のJSONだけを返してください（前置き・コードブロック禁止）:
{"direction":"positive|negative|neutral","category":"上方修正|増配|自社株買い|提携|分割|受注|TOB|その他","confidence":0.0-1.0,"summary":"30字以内で根拠"}

判定基準（保守的に）:
- 業績予想の修正: 営業利益予想の上方幅が+5%未満、または売上のみ上方で利益は据え置き/下方 → neutral
- 自社株買い: 取得上限が発行済株式の1%未満 → neutral
- 増配: 記念配当のみで普通配当据え置き → neutral
- 提携: 金額・具体性が無い「検討開始」レベル → neutral
- 既に公表済みの内容の再掲・訂正・進捗報告 → neutral"""


def ai_judge(item: Disclosure, pdf_text: str):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        body = pdf_text[: config.AI_PDF_CHARS] if pdf_text else "（本文取得失敗。タイトルのみで判定）"
        msg = client.messages.create(
            model=config.AI_MODEL,
            max_tokens=200,
            system=AI_SYSTEM,
            messages=[{"role": "user", "content":
                       f"会社: {item.name}({item.code4})\nタイトル: {item.title}\n\n本文:\n{body}"}],
        )
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", raw, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception as e:
        print(f"[judge] ai error {item.code4}: {e}")
        return None


def judge(item: Disclosure):
    kw = keyword_judge(item.title)
    if kw is None:
        return None
    use_ai = bool(os.environ.get("ANTHROPIC_API_KEY"))
    need_pdf = bool(kw["ambiguous"]) or (use_ai and config.AI_JUDGE_ALL)
    pdf_text = fetch_pdf_text(item.url) if need_pdf else ""

    if use_ai and need_pdf:
        ai = ai_judge(item, pdf_text)
        if ai is not None:
            if ai.get("direction") != "positive":
                return None
            kw["ai"] = ai
            if not kw["labels"]:
                kw["labels"] = [ai.get("category", "AI判定")]
            kw["score"] = max(kw["score"], 2)
            return kw
        # AI失敗 → 下のヒューリスティックへフォールバック

    if kw["ambiguous"] and not kw["labels"]:
        d = pdf_direction(pdf_text)
        if d == "negative":
            return None
        if d == "positive":
            kw["labels"] = ["上方修正（本文推定）"]
            kw["score"] = 2
        else:
            kw["labels"] = ["方向未確認"]
            kw["note"] = "PDFで上方/下方を要確認"
            kw["score"] = 1
    return kw
