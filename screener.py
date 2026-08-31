"""yfinance の日足でチャート条件を判定する。

条件（すべて満たすと passed）:
  1. 現在値 > 25日MA > 75日MA（上昇トレンド）
  2. RSI14 < RSI_MAX（高値掴み回避）
  3. 当日出来高 ≥ 20日平均 × VOLUME_RATIO（場中は経過時間で按分）
  4. 前日終値比のギャップ ≤ MAX_GAP_PCT

1 の「現在値 > 25MA」は require_above_ma25=False で外せる（教材スキャン用。
押し目からの反発は定義上いったん 25MA を割るため、そこを狙う枠では
25MA > 75MA だけを見る）。

3 は引け後（15:30以降）には課さない。引け後に出た開示の当日出来高は
「ニュースが出る前」に積み上がった数字で、材料はまだ売買されていない。
1.5倍を要求すると夜間の監視が構造的に空振りするため、表示だけして
合否からは外す（require_volume で明示的に上書きもできる）。
"""
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

import config

JST = ZoneInfo("Asia/Tokyo")
SESSION_MIN = 330  # 9:00-11:30 (150分) + 12:30-15:30 (180分)


@dataclass
class Screen:
    passed: bool
    reasons: list = field(default_factory=list)   # 不合格理由
    price: float = 0.0
    prev_close: float = 0.0
    gap_pct: float = 0.0
    ma25: float = 0.0
    ma75: float = 0.0
    rsi: float = 0.0
    vol_ratio: float = 0.0
    atr: float = 0.0
    stop: float = 0.0
    entry_label: str = ""
    after_close: bool = False


def session_fraction(now: datetime) -> float:
    """当日の立会時間のうち経過した割合（0〜1）。"""
    t = now.time()
    def mins(a, b):
        return (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute)
    if t < dtime(9, 0):
        return 0.0
    if t < dtime(11, 30):
        return mins(dtime(9, 0), t) / SESSION_MIN
    if t < dtime(12, 30):
        return 150 / SESSION_MIN
    if t < dtime(15, 30):
        return (150 + mins(dtime(12, 30), t)) / SESSION_MIN
    return 1.0


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    au = up.ewm(alpha=1 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + au / ad.replace(0, 1e-9))


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df["Close"].shift(1)
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - pc).abs(), (df["Low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def evaluate(df: pd.DataFrame, now: datetime, require_volume: bool | None = None,
             require_above_ma25: bool = True) -> Screen:
    """ネットワーク不要の純粋な判定ロジック（テスト用に分離）。

    require_volume: 出来高条件を課すか。None（既定）は「引け後は課さない」。
    require_above_ma25: 1 の「現在値 > 25MA」まで課すか。False なら 25MA > 75MA
        だけを見る。押し目からの反発（教材の追随期）は定義上いったん 25MA を
        割るので、そこを狙う教材スキャンでは False にする。
    """
    df = df.dropna(subset=["Close"])
    if len(df) < config.MIN_HISTORY:
        return Screen(False, [f"日足不足({len(df)}本)"])

    last_date = df.index[-1].date()
    is_today = last_date == now.date()
    close, vol = df["Close"], df["Volume"]

    s = Screen(True)
    s.price = float(close.iloc[-1])
    s.prev_close = float(close.iloc[-2])
    s.gap_pct = (s.price / s.prev_close - 1) * 100
    s.ma25 = float(close.rolling(25).mean().iloc[-1])
    s.ma75 = float(close.rolling(75).mean().iloc[-1])
    s.rsi = float(rsi(close).iloc[-1])
    s.atr = float(atr(df).iloc[-1])
    s.stop = s.price - config.ATR_STOP_MULT * s.atr
    s.after_close = now.time() >= dtime(15, 30)
    if require_volume is None:
        require_volume = not s.after_close

    if not is_today:
        s.reasons.append("当日株価未取得")
        s.vol_ratio = 0.0
    else:
        avg20 = float(vol.iloc[-21:-1].mean())
        frac = max(session_fraction(now), 0.15)      # 寄付き直後はノイズが大きいので最低15%扱い
        expected = avg20 * frac if avg20 > 0 else 0
        s.vol_ratio = float(vol.iloc[-1]) / expected if expected else 0.0
        if require_volume and s.vol_ratio < config.VOLUME_RATIO:
            s.reasons.append(f"出来高{s.vol_ratio:.1f}倍<{config.VOLUME_RATIO}")

    if require_above_ma25 and not (s.price > s.ma25 > s.ma75):
        s.reasons.append("トレンド不成立(現在値>25MA>75MA)")
    elif not require_above_ma25 and not (s.ma25 > s.ma75):
        s.reasons.append("トレンド不成立(25MA>75MA)")
    if s.rsi >= config.RSI_MAX:
        s.reasons.append(f"RSI{s.rsi:.0f}≥{config.RSI_MAX}")
    if s.gap_pct > config.MAX_GAP_PCT:
        s.reasons.append(f"ギャップ{s.gap_pct:+.1f}%>+{config.MAX_GAP_PCT:g}%")

    s.entry_label = ("エントリー候補" if s.gap_pct <= config.ENTRY_GAP_PCT
                     else "様子見（押し目待ち）")
    s.passed = not s.reasons
    return s


def fetch_history(code4: str, retries: int = 2):
    import yfinance as yf
    for i in range(retries + 1):
        try:
            df = yf.Ticker(f"{code4}.T").history(period=config.HISTORY_PERIOD,
                                                 interval="1d", auto_adjust=False)
            if df is not None and len(df):
                return df
        except Exception as e:
            print(f"[screener] {code4} fetch error: {e}")
        time.sleep(2 * (i + 1))
    return None


def _slice_batch(data, ticker: str):
    """yf.download の戻りから1銘柄分を切り出す。取れなければ None。"""
    if data is None or not len(data):
        return None
    try:
        df = data[ticker] if isinstance(data.columns, pd.MultiIndex) else data
        df = df.dropna(subset=["Close"])
    except (KeyError, IndexError):
        return None
    return df if len(df) else None


def fetch_history_batch(code4s: list[str], chunk: int | None = None) -> dict:
    """複数銘柄の日足をまとめて取る。{code4: DataFrame or None}。

    1銘柄ずつ叩くと 225 銘柄で 4 分以上かかるため、yf.download でまとめて取る。
    チャンク単位で失敗しても、その分が None になるだけで全体は続行する。
    """
    import yfinance as yf
    chunk = chunk or config.BATCH_CHUNK
    out: dict = {}
    for i in range(0, len(code4s), chunk):
        part = code4s[i:i + chunk]
        tickers = [f"{c}.T" for c in part]
        data = None
        try:
            data = yf.download(tickers, period=config.HISTORY_PERIOD, interval="1d",
                               auto_adjust=False, group_by="ticker", threads=True,
                               progress=False)
        except Exception as e:
            print(f"[screener] batch fetch error {part[0]}-{part[-1]}: {e}")
        for c, t in zip(part, tickers):
            out[c] = _slice_batch(data, t)
        if i + chunk < len(code4s):
            time.sleep(1.0)   # yfinance レート制限対策
    return out


def fetch_market():
    """地合い判定用の指数日足。失敗しても None で続行（地合い行が出ないだけ）。"""
    import yfinance as yf
    try:
        df = yf.Ticker(config.MARKET_INDEX).history(period="1y", interval="1d", auto_adjust=False)
        return df if df is not None and len(df) else None
    except Exception as e:
        print(f"[screener] market fetch error: {e}")
        return None


def next_earnings_date(code4: str):
    """次回の決算発表日（date）。yfinance のベストエフォット情報なので取れなければ None。"""
    import yfinance as yf
    try:
        ed = yf.Ticker(f"{code4}.T").get_earnings_dates(limit=8)
        if ed is None or not len(ed):
            return None
        today = datetime.now(JST).date()
        future = [ts.date() for ts in ed.index if ts.date() >= today]
        return min(future) if future else None
    except Exception:
        return None


def screen(code4: str, now: datetime | None = None):
    now = now or datetime.now(JST)
    df = fetch_history(code4)
    if df is None:
        return None
    return evaluate(df, now)
