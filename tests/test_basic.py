"""ネットワーク不要のテスト。  python tests/test_basic.py"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd

import config
from tdnet import parse_list
from judge import keyword_judge, pdf_direction
from screener import evaluate, session_fraction

JST = ZoneInfo("Asia/Tokyo")

# --- TDnet パース ---
items = parse_list(open("sample/tdnet_sample.html", encoding="utf-8").read(), date(2026, 8, 30))
assert len(items) == 6, len(items)
assert items[0].code4 == "7203" and items[0].url.endswith("140120260830500001.pdf")
assert items[0].id == "140120260830500001.pdf"

# --- キーワード判定 ---
r = {it.code4: keyword_judge(it.title) for it in items}
assert r["7203"]["labels"] == ["上方修正"]
assert r["6758"]["labels"] == ["自己株式の取得"]
assert r["9843"] is None                      # 取得状況 → スキップ
assert r["9984"]["labels"] == [] and r["9984"]["ambiguous"]   # 方向不明
assert r["4502"] is None                      # 役員 → スキップ
assert "増配" in r["8035"]["labels"]

# --- PDF 方向推定 ---
assert pdf_direction("通期の業績予想を上方修正いたします。売上高は前回予想を上回る見込み") == "positive"
assert pdf_direction("業績予想を下方修正いたします") == "negative"
assert pdf_direction("") is None

# --- 立会時間の按分 ---
mk = lambda h, m: datetime(2026, 8, 31, h, m, tzinfo=JST)
assert session_fraction(mk(8, 0)) == 0.0
assert abs(session_fraction(mk(10, 0)) - 60 / 330) < 1e-9
assert abs(session_fraction(mk(12, 0)) - 150 / 330) < 1e-9
assert session_fraction(mk(16, 0)) == 1.0

# --- チャート判定（合成データ：上昇トレンド＋当日出来高急増）---
def make_df(today, n=120, trend=1.0, last_vol_mult=3.0, gap=1.5):
    idx = pd.bdate_range(end=today, periods=n, tz="Asia/Tokyo")
    rng = np.random.default_rng(8)
    close = 1000 + np.cumsum(rng.normal(trend * 1.0, 8.0, n))   # 緩やかな上昇（RSIが70未満に収まる）
    close[-1] = close[-2] * (1 + gap / 100)
    vol = np.full(n, 100_000.0); vol[-1] = 100_000 * last_vol_mult
    df = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Volume": vol}, index=idx)
    return df

today = date(2026, 8, 31)
now = datetime(2026, 8, 31, 10, 0, tzinfo=JST)   # 60分経過 → 按分 0.18
s = evaluate(make_df(today, last_vol_mult=1.0), now)     # 当日出来高が日平均と同じ＝按分後 5.5倍
assert s.passed, s.reasons
assert s.entry_label == "エントリー候補" and s.stop < s.price
s = evaluate(make_df(today, gap=6.0), now)               # ギャップ大
assert not s.passed and any("ギャップ" in x for x in s.reasons)
s = evaluate(make_df(today, trend=-1.0), now)            # 下降トレンド
assert not s.passed and any("トレンド" in x for x in s.reasons)
s = evaluate(make_df(today, last_vol_mult=0.05), now)    # 出来高不足
assert not s.passed and any("出来高" in x for x in s.reasons)
s = evaluate(make_df(today - timedelta(days=3)), now)    # 当日株価なし（前営業日=金曜まで）
assert not s.passed and "当日株価未取得" in s.reasons
s = evaluate(make_df(today, gap=3.0), datetime(2026, 8, 31, 16, 0, tzinfo=JST))
assert s.after_close and s.entry_label == "様子見（押し目待ち）"

print("all tests passed")
