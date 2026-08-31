"""ネットワーク不要のテスト。  python tests/test_basic.py"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd

import config
import main as main_module   # import時エラー（型注釈のNameError等）をCIで検出する
import notify as notify_module
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

# --- 教材由来のチャート文脈判定（chart_context） ---
from chart_context import (analyze, context_lines, earnings_note, market_condition,
                           stance, trading_days_until)


def make_path(*segs, end=date(2026, 8, 31)):
    """(始値, 終値, 日数) の区間を連結した終値系列から日足DataFrameを作る。"""
    closes = []
    for a, b, n in segs:
        closes += list(np.linspace(a, b, n, endpoint=False))
    closes.append(segs[-1][1])
    arr = np.array(closes)
    idx = pd.bdate_range(end=end, periods=len(arr), tz="Asia/Tokyo")
    op = np.concatenate(([arr[0]], arr[:-1]))
    return pd.DataFrame({"Open": op, "High": np.maximum(arr, op) * 1.005,
                         "Low": np.minimum(arr, op) * 0.995, "Close": arr,
                         "Volume": np.full(len(arr), 100_000.0)}, index=idx)


# 上昇トレンド → 抵抗線1200上抜け → 1ヶ月以上あけて押し目 → 反発（ロールリバーサル成立）
roll = make_path((900, 1050, 50), (1050, 950, 30), (950, 1200, 100),
                 (1200, 1030, 60), (1030, 1290, 100), (1290, 1215, 30), (1215, 1230, 3))
ctx = analyze(roll)
assert ctx.ok and ctx.trend == "上昇", (ctx.trend, ctx)
assert ctx.rolled and ctx.stage == "追随期", (ctx.stage, ctx.days_since_break)
assert ctx.support and 1190 < ctx.support < 1220          # 上抜けた目立つ山 ≒1200
assert ctx.resistance and 1280 < ctx.resistance < 1310    # 次の目立つ山 ≒1290 = 利確目安
assert ctx.days_since_break >= config.PULLBACK_MIN_DAYS
lines = context_lines(ctx)
assert any("ステージ 追随期" in l for l in lines) and any("上昇トレンド" in l for l in lines)
assert any("利確目安" in l for l in lines)
assert "教材条件" in stance(ctx, "良好")

# 高値・安値切り下げの下降トレンド → 先行期（買わない）
down = make_path((1200, 1300, 30), (1300, 1100, 50), (1100, 1250, 30),
                 (1250, 1000, 50), (1000, 1080, 20))
ctx = analyze(down)
assert ctx.trend == "下降" and ctx.stage == "先行期", (ctx.trend, ctx.stage)
assert "下降トレンド" in ctx.warnings

# ブレイク直後（1ヶ月未満・押し目未形成）→ 利食い期（飛びつき注意）
fresh = make_path((900, 1050, 50), (1050, 950, 30), (950, 1200, 100),
                  (1200, 1030, 60), (1030, 1225, 75))
ctx = analyze(fresh)
assert ctx.stage == "利食い期" and not ctx.rolled, (ctx.stage, ctx.days_since_break)
assert ctx.days_since_break is not None and ctx.days_since_break < config.PULLBACK_MIN_DAYS
assert "飛びつき買い注意" in ctx.warnings

# 日足不足 → 判定不能
ctx = analyze(make_path((1000, 1100, 30)))
assert not ctx.ok and "判定不能" in stance(ctx)

# 地合い: ダブルトップ＋ネックライン割れ → 悪化 ／ 一本調子の上昇 → 良好 ／ データ無し → ""
label, detail = market_condition(make_path((30000, 33000, 60), (33000, 31000, 30),
                                           (31000, 33100, 60), (33100, 30500, 50)))
assert label == "悪化" and "天井サイン" in detail, (label, detail)
label, detail = market_condition(make_path((30000, 36000, 200)))
assert label == "良好", (label, detail)
assert market_condition(None)[0] == ""

# 決算接近の注記（2026/8/31(月) 起点）
assert trading_days_until(date(2026, 8, 31), date(2026, 9, 3)) == 3
note = earnings_note(date(2026, 9, 3), date(2026, 8, 31))
assert "決算 09/03" in note and "決算またぎ回避" in note
note = earnings_note(date(2026, 10, 30), date(2026, 8, 31))
assert "決算 10/30" in note and "決算またぎ回避" not in note
assert earnings_note(None, date(2026, 8, 31)) == ""

# --- 銘柄状況レポート（--stock） ---
import stock_info
from stock_info import fmt_stock, normalize_code, parse_codes, yen

assert yen(1234.0) == "1,234" and yen(1234.5) == "1,234.5" and yen(float("nan")) == "-"
assert normalize_code(" 7203 ") == "7203"
assert normalize_code("72030") == "7203"                       # TDnet式5桁
assert normalize_code("130a") == "130A"                        # 英字入りの新方式コード
assert normalize_code("999") is None and normalize_code("72031") is None
assert normalize_code("ABCD") is None
assert parse_codes("7203, 6758、9984 7203") == ["7203", "6758", "9984"]

s = evaluate(make_df(today, last_vol_mult=1.0), now)
txt = fmt_stock("7203", "トヨタ自動車", s, today, today, [" 開示 なし（当日＋前営業日）"])
assert txt.startswith("■ 7203 トヨタ自動車")
assert "チャート条件 合格 → エントリー候補" in txt and "開示 なし" in txt
assert "トレンド 上昇" in txt and "損切り目安" in txt
txt = fmt_stock("7203", "トヨタ自動車", evaluate(make_df(today, trend=-1.0), now), today, today)
assert "チャート条件 未達" in txt and "上昇トレンド不成立" in txt
txt = fmt_stock("7203", "トヨタ自動車", evaluate(make_df(today - timedelta(days=3)), now),
                today - timedelta(days=3), today)
assert "（08/28終値）" in txt and "出来高 -" in txt              # 休場日は直近終値で表示
txt = fmt_stock("1305", "", None)
assert txt.startswith("■ 1305\n") and "株価データ無し" in txt

# 教材判定付きのレポート（合格ケースに文脈行が付く）
s = evaluate(make_df(today, last_vol_mult=1.0), now)
txt = fmt_stock("7203", "トヨタ自動車", s, today, today, None, analyze(roll),
                earnings_note(date(2026, 9, 3), today), "良好")
assert "ステージ 追随期" in txt and "総合 " in txt and "決算またぎ回避" in txt

# stock_report をネットワーク無しで一気通貫（取得系を差し替え）
stock_info.fetch_history = lambda c: make_df(today, last_vol_mult=1.0)
stock_info.fetch_name = lambda c: "テスト株式会社"
stock_info.fetch_day = lambda d: parse_list(
    open("sample/tdnet_sample.html", encoding="utf-8").read(), d)
stock_info.fetch_market = lambda: make_path((30000, 36000, 200))
stock_info.next_earnings_date = lambda c: None
rep = stock_info.stock_report("7203, xxxx", now, disclosure_days=[today])
assert "【銘柄状況】08/31 10:00" in rep
assert "地合い 日経平均 良好" in rep
assert "■ 7203 テスト株式会社" in rep and "〔上方修正〕" in rep
assert "ステージ " in rep and "総合 " in rep
assert "■ xxxx\n 無効なコード" in rep
assert rep.rstrip().endswith("※判断材料であり売買の推奨ではない")

print("all tests passed")
