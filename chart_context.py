"""教材（水平ロールリバーサル手法）由来のチャート文脈判定。

日足OHLCVのDataFrame（yfinance形式）だけを入力に、ネットワーク無しで判定する。
既存のチャート条件（screener.py の合否ゲート）は変えず、通知に併記する
「投資判断の材料」を増やすためのモジュール。

コード化した教材ルール:
  - 目立つ山・谷 = 週足ピボット（前後 PIVOT_SPAN_W 週より高い/安い点）。日足だけの小さな山は無視
  - ダウ理論: 直近2つの目立つ高値・安値がともに切り上げ→上昇 / 切り下げ→下降
  - ステージ: 先行期（買わない）/ 追随期（唯一の買い場）/ 利食い期（飛びつき注意）
  - 水平ロールリバーサル: 抵抗線上抜け → PULLBACK_MIN_DAYS 営業日以上あけてライン再接近 → 反発確認
  - 上値抵抗までの余地（次の目立つ山＝利確目安）と真上の抵抗線警告
  - 25MAの傾き（下向きのラインタッチは買わない）
  - 指数の地合い: 25MAとの位置・天井サイン（ダブルトップ＋ネックライン割れ）で新規買いの追い風/逆風
  - 決算接近の警告（決算またぎ回避）
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

import jpholiday
import pandas as pd

import config


def yen(p) -> str:
    if p is None or p != p:  # None / NaN
        return "-"
    return f"{p:,.0f}" if p == int(p) else f"{p:,.1f}"


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5 or jpholiday.is_holiday(d):
        return False
    if (d.month == 12 and d.day == 31) or (d.month == 1 and d.day <= 3):
        return False
    return True


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def trading_days_until(d0: date, d1: date) -> int:
    """d0 の翌日から d1 までの営業日数（d1 が営業日なら d1 も数える）。"""
    n, d = 0, d0
    while d < d1 and n < 260:
        d += timedelta(days=1)
        if is_trading_day(d):
            n += 1
    return n


@dataclass
class Context:
    ok: bool = True
    trend: str = ""                    # 上昇 / 下降 / 横ばい / ""=判定不能
    stage: str = ""                    # 先行期 / 追随期 / 利食い期 / 中立
    stage_note: str = ""
    ma25_up: bool = False
    support: float | None = None       # 直近に上抜けた目立つ山（ローリバの水平線）
    resistance: float | None = None    # 現在値の上にある最も近い目立つ山（利確目安）
    room_pct: float | None = None      # 上値抵抗までの余地%
    new_high: bool = False             # 取得期間内の高値を更新中
    rolled: bool = False               # 押し目反発（ロールリバーサル）確認済み
    days_since_break: int | None = None
    warnings: list = field(default_factory=list)


def _weekly(daily: pd.DataFrame) -> pd.DataFrame:
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    return daily.resample("W-FRI").agg(agg).dropna(subset=["Close"])


def _pivots(vals: pd.Series, span: int, high: bool = True) -> list[tuple[object, float]]:
    """前後 span 本より高い（安い）点。末尾 span 本は未確定なので含まれない。"""
    v = vals.to_numpy(dtype=float)
    out = []
    for i in range(span, len(v) - span):
        others = list(v[i - span:i]) + list(v[i + 1:i + span + 1])
        if high and v[i] > max(others):
            out.append((vals.index[i], v[i]))
        elif not high and v[i] < min(others):
            out.append((vals.index[i], v[i]))
    return out


def analyze(df: pd.DataFrame) -> Context:
    ctx = Context()
    daily = df.dropna(subset=["Close"])
    if len(daily) < config.MIN_HISTORY:
        ctx.ok = False
        return ctx
    close = daily["Close"]
    price = float(close.iloc[-1])

    w = _weekly(daily)
    ph = _pivots(w["High"], config.PIVOT_SPAN_W, high=True)
    pl = _pivots(w["Low"], config.PIVOT_SPAN_W, high=False)
    if len(ph) >= 2 and len(pl) >= 2:
        hh, hl = ph[-1][1] > ph[-2][1], pl[-1][1] > pl[-2][1]
        ctx.trend = "上昇" if hh and hl else ("下降" if not hh and not hl else "横ばい")

    levels = sorted(p for _, p in ph)
    below = [p for p in levels if p < price]
    above = [p for p in levels if p >= price]
    ctx.support = below[-1] if below else None
    ctx.resistance = above[0] if above else None
    if ctx.resistance is not None:
        ctx.room_pct = (ctx.resistance / price - 1) * 100
    ctx.new_high = not above and len(daily) > 1 and price >= float(daily["High"].iloc[:-1].max())

    ma25 = close.rolling(25).mean()
    if len(ma25.dropna()) > config.MA_SLOPE_DAYS:
        ctx.ma25_up = bool(ma25.iloc[-1] > ma25.iloc[-1 - config.MA_SLOPE_DAYS])

    near_pct = 1 + config.LINE_NEAR_PCT / 100
    if ctx.support is not None:
        L = ctx.support
        ab = (close > L).to_numpy()
        cross = [i for i in range(1, len(ab)) if ab[i] and not ab[i - 1]]
        if cross and ab[-1]:
            t0 = cross[-1]
            ctx.days_since_break = len(ab) - 1 - t0
            lows = daily["Low"].to_numpy(dtype=float)
            touches = [i for i in range(t0 + 1, len(ab)) if lows[i] <= L * near_pct]
            deep = [i for i in touches if i - t0 >= config.PULLBACK_MIN_DAYS]
            last = daily.iloc[-1]
            bounce = (float(last["Close"]) > float(last["Open"])
                      and price > float(close.iloc[-2]) and price > L)
            # 押し目はブレイクから1ヶ月以上・直近5営業日以内、当日は陽線で反発していること
            ctx.rolled = bool(deep) and (len(ab) - 1 - deep[-1] <= 5) and bounce

    if ctx.trend == "下降":
        ctx.stage, ctx.stage_note = "先行期", "下降トレンド中は見送り・高値と安値が切り下げ"
    elif ctx.rolled:
        ctx.stage, ctx.stage_note = "追随期", "押し目反発を確認・水平ロールリバーサル成立"
    elif ctx.days_since_break is not None and ctx.days_since_break < config.PULLBACK_MIN_DAYS:
        ctx.stage, ctx.stage_note = "利食い期", \
            f"ブレイク後{ctx.days_since_break}営業日・初押し{config.PULLBACK_MIN_DAYS}営業日以上待ち"
    elif ctx.support is not None and price <= ctx.support * near_pct:
        ctx.stage, ctx.stage_note = "先行期", "支持線に接近中・反発の確認待ち"
    elif ctx.new_high:
        ctx.stage, ctx.stage_note = "利食い期", "新高値圏を上伸中・飛びつかず初押し待ち"
    else:
        ctx.stage, ctx.stage_note = "中立", "押し目形成待ち"

    if ctx.trend == "下降":
        ctx.warnings.append("下降トレンド")
    if ctx.stage == "利食い期":
        ctx.warnings.append("飛びつき買い注意")
    if ctx.room_pct is not None and ctx.room_pct < config.RESISTANCE_ROOM_PCT:
        ctx.warnings.append(f"真上に抵抗線（余地{ctx.room_pct:+.1f}%）")
    if not ctx.ma25_up:
        ctx.warnings.append("25MA下向き")
    return ctx


def market_condition(df: pd.DataFrame | None) -> tuple[str, str]:
    """指数（日経平均）の地合い。(ラベル, 根拠) を返す。ラベル "" = 判定不能。"""
    if df is None:
        return "", "指数データ取得失敗"
    d = df.dropna(subset=["Close"])
    if len(d) < 30:
        return "", "指数データ不足"
    close = d["Close"]
    price = float(close.iloc[-1])
    ma25 = close.rolling(25).mean()
    above = price > float(ma25.iloc[-1])
    rising = float(ma25.iloc[-1]) > float(ma25.iloc[-1 - config.MA_SLOPE_DAYS])

    w = _weekly(d)
    ph = _pivots(w["High"], config.PIVOT_SPAN_W, high=True)
    if len(ph) >= 2:
        (t1, p1), (t2, p2) = ph[-2], ph[-1]
        if abs(p2 - p1) / p1 <= config.DOUBLE_TOP_TOL_PCT / 100:
            neck = float(w["Low"].loc[t1:t2].min())
            if price < neck:
                return "悪化", "天井サイン（ダブルトップ＋ネックライン割れ）"
    if above and rising:
        return "良好", "終値>25MA・25MA上向き"
    if not above and not rising:
        return "悪化", "終値<25MA・25MA下向き"
    return "注意", "終値と25MAの方向が混在"


def earnings_note(edate: date | None, today: date) -> str:
    """決算日の注記行。日付が取れないときは ""（行を出さない）。"""
    if edate is None:
        return ""
    n = trading_days_until(today, edate)
    line = f"決算 {edate:%m/%d} 予定（あと{n}営業日）"
    if n <= config.EARNINGS_WARN_DAYS:
        line += " ⚠決算またぎ回避"
    return line


def stance_score(ctx: Context, market_label: str = "") -> tuple[int, int]:
    """教材条件の (充足数, 項目数)。市場ラベルが空なら地合いの項目は数えない。

    判定不能（日足不足）は (0, 0) を返す。満点判定は n == total > 0 で見る。
    """
    if not ctx.ok:
        return 0, 0
    checks = [
        ctx.trend == "上昇",
        ctx.stage == "追随期",
        ctx.ma25_up,
        ctx.new_high or (ctx.room_pct is not None and ctx.room_pct >= config.RESISTANCE_ROOM_PCT),
    ]
    if market_label:
        checks.append(market_label == "良好")
    return sum(checks), len(checks)


def scan_ok(ctx: Context, market_label: str = "") -> bool:
    """教材スキャン（材料ニュース無しで通知する枠）の合否。

    要求するのは「上昇トレンド・追随期・25MA上向き・地合い良好」の4点。
    上値余地はゲートにせず room_line() で数値を出すだけにする。教材の
    「追随期＝押し目からの反発」で入る以上、直上の節目までの距離は銘柄ごとに
    大きく違い、一律のしきい値で切ると通知がほぼ出なくなるため（合成株価
    3000件では満点を要求すると成立率0.03%＝229銘柄で週0.4件しか出なかった）。
    上値余地も課したいときは config.SCAN_REQUIRE_ROOM = True にする。
    """
    if not ctx.ok:
        return False
    checks = [ctx.trend == "上昇", ctx.stage == "追随期", ctx.ma25_up]
    if config.SCAN_REQUIRE_ROOM:
        checks.append(ctx.new_high or (ctx.room_pct is not None
                                       and ctx.room_pct >= config.RESISTANCE_ROOM_PCT))
    if market_label:
        checks.append(market_label == "良好")
    return all(checks)


def room_line(ctx: Context, price: float, stop: float) -> str:
    """上値余地（次の目立つ山＝利確目安までの伸びしろ）と損切りまでの距離。

    上値余地をスキャンのゲートから外した代わりに、円と%とリスクリワード比で
    出して「その余地で取りに行くか」を自分で判断できるようにする。
    """
    if not price:
        return ""
    risk_pct = (price - stop) / price * 100 if stop and price > stop else 0.0
    risk = (f"損切りまで −{risk_pct:.1f}%（−{yen(price - stop)}円）" if risk_pct > 0
            else "損切りまで −")
    if ctx.new_high:
        return f" 上値余地 － ／ {risk}（新高値圏・取得期間内に上値の節目なし）"
    if ctx.resistance is None or ctx.room_pct is None:
        return f" 上値余地 不明 ／ {risk}（上に目立つ山が無い）"
    up = (f"上値余地 +{ctx.room_pct:.1f}%"
          f"（{yen(ctx.resistance)}円まで {yen(ctx.resistance - price)}円）")
    if risk_pct <= 0:
        return f" {up} ／ {risk}"
    return f" {up} ／ {risk} ／ リスクリワード {ctx.room_pct / risk_pct:.1f}"


def stance(ctx: Context, market_label: str = "") -> str:
    """教材条件の充足数から総合評価をつくる（判断材料。売買推奨ではない）。"""
    if not ctx.ok:
        return "判定不能（日足データ不足）"
    n, total = stance_score(ctx, market_label)
    mark = ("◎" if n == total else "○" if n == total - 1
            else "△" if n == total - 2 else "▲")
    label = {"◎": "追い風", "○": "おおむね良好", "△": "条件不足", "▲": "見送り寄り"}[mark]
    return f"{mark} {label}（教材条件 {n}/{total}）"


def context_lines(ctx: Context) -> list[str]:
    """通知本文に足す教材判定の行。fmt_hit / fmt_stock 共用。"""
    if not ctx.ok:
        return [" 教材判定 不能（日足データ不足）"]
    out = [f" ステージ {ctx.stage}（{ctx.stage_note}）"]
    trend = f"{ctx.trend}トレンド" if ctx.trend else "トレンド判定不能"
    out.append(f" 週足 {trend} ／ 25MA {'上向き' if ctx.ma25_up else '下向き'}")
    sup = f"支持線 {yen(ctx.support)}" if ctx.support is not None else "支持線 −"
    if ctx.new_high:
        res = "上値抵抗 なし（新高値圏）"
    elif ctx.resistance is not None:
        res = f"上値抵抗 {yen(ctx.resistance)}（余地 {ctx.room_pct:+.1f}%・利確目安）"
    else:
        res = "上値抵抗 −"
    out.append(f" 節目 {sup} ／ {res}")
    if ctx.warnings:
        out.append(" 注意 " + "／".join(ctx.warnings))
    return out
