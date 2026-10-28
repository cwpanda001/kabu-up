"""判断数値（プロが売買の前に必ず確認する数字）を通知に足す。

チャート条件や教材判定が「買ってよい形か」を見るのに対し、ここは
「その銘柄を実際に売買するときの数字」を出す。合否には使わない（併記のみ）。

  判断数値 売買代金 52億円/日 ／ 25MA乖離 +2.5% ／ 対日経 +4.3pt（20日）／ 52週高値まで −3.1%
  指標     時価総額 4.6兆円 ／ PER 12.3倍 ／ PBR 1.10倍 ／ 配当利回り 2.80% ／ ROE 9.5%
  100株    必要資金 28.5万円 ／ 損切りで −1.3万円 ／ 利確目安で +2.3万円

- 売買代金（20日平均）: 流動性。少ないと思った値段で売買できない。LIQUIDITY_WARN_OKU 未満で警告
- 25MA乖離: 平均からの離れ具合。大きくプラスなら短期の過熱
- 対日経（相対強度）: 20営業日の騰落率 − 日経平均の騰落率。市場全体より強い株か
- 52週高値まで: 1年の高値からの距離。高値に近いほど上値の売り圧力（しこり）が少ない
- 指標（PER・PBR・配当利回り・ROE・時価総額）: yfinance の銘柄情報。取れない項目は出さない
- 100株: 日本株の売買単位（単元）あたりの必要資金と、損切り・利確目安に届いたときの損益

ネットワークを使うのは fetch_valuation() だけ（ヒットした銘柄にだけ呼ぶ）。
"""
from functools import lru_cache

import pandas as pd

import config


def _man(v: float) -> str:
    """円を「x.x万円」で。1万円未満は円のまま。"""
    if abs(v) >= 10_000:
        return f"{v / 10_000:,.1f}万円"
    return f"{v:,.0f}円"


def _oku(v: float) -> str:
    """円を「x億円」「x.x兆円」で（時価総額・売買代金）。"""
    oku = v / 1e8
    if oku >= 10_000:
        return f"{oku / 10_000:,.1f}兆円"
    if oku >= 10:
        return f"{oku:,.0f}億円"
    return f"{oku:,.1f}億円"


def pro_line(df: pd.DataFrame, idx_df: pd.DataFrame | None = None) -> str:
    """売買代金・25MA乖離・対日経・52週高値までの1行。日足が足りなければ ""。"""
    d = df.dropna(subset=["Close"])
    if len(d) < 26:
        return ""
    close, vol = d["Close"], d["Volume"]
    price = float(close.iloc[-1])
    parts = []

    # 当日（場中）の出来高は途中の値なので、確定した直近20日で平均を取る
    turnover = float((close.iloc[-21:-1] * vol.iloc[-21:-1]).mean())
    if turnover == turnover and turnover > 0:
        t = f"売買代金 {_oku(turnover)}/日"
        if turnover / 1e8 < config.LIQUIDITY_WARN_OKU:
            t += "（⚠流動性低）"
        parts.append(t)

    ma25 = float(close.rolling(25).mean().iloc[-1])
    if ma25:
        parts.append(f"25MA乖離 {(price / ma25 - 1) * 100:+.1f}%")

    n = config.RS_DAYS
    if idx_df is not None and len(d) > n:
        ic = idx_df.dropna(subset=["Close"])["Close"]
        if len(ic) > n:
            mine = (price / float(close.iloc[-1 - n]) - 1) * 100
            idx = (float(ic.iloc[-1]) / float(ic.iloc[-1 - n]) - 1) * 100
            parts.append(f"対日経 {mine - idx:+.1f}pt（{n}日）")

    high = float(d["High"].iloc[-250:].max())
    if high == high and high > 0:
        if price >= high:
            parts.append("52週高値 更新中")
        else:
            parts.append(f"52週高値まで {(price / high - 1) * 100:+.1f}%")
    return " 判断数値 " + " ／ ".join(parts) if parts else ""


@lru_cache(maxsize=256)
def fetch_valuation(code4: str) -> dict:
    """PER・PBR などの銘柄情報（yfinance のベストエフォット）。取れなければ {}。

    同じ実行で同じ銘柄を何度も引かないようキャッシュする（レート制限対策）。
    """
    if not config.PRO_VALUATION:
        return {}
    try:
        import yfinance as yf
        return dict(yf.Ticker(f"{code4}.T").info or {})
    except Exception as e:
        print(f"[metrics] {code4} info error: {e}")
        return {}


def _num(info: dict, *keys) -> float | None:
    for k in keys:
        v = info.get(k)
        if isinstance(v, (int, float)) and v == v and v > 0:
            return float(v)
    return None


def valuation_line(info: dict, price: float) -> str:
    """時価総額・PER・PBR・配当利回り・ROE の1行。1つも取れなければ ""。"""
    if not info or not price:
        return ""
    parts = []
    mcap = _num(info, "marketCap")
    if mcap:
        parts.append(f"時価総額 {_oku(mcap)}")
    per = _num(info, "trailingPE")
    if per is None and _num(info, "trailingEps"):
        per = price / _num(info, "trailingEps")
    if per:
        parts.append(f"PER {per:.1f}倍")
    pbr = _num(info, "priceToBook")
    if pbr is None and _num(info, "bookValue"):
        pbr = price / _num(info, "bookValue")
    if pbr:
        parts.append(f"PBR {pbr:.2f}倍")
    # dividendYield は yfinance の版によって比率（0.028）と%（2.8）が混在するので、
    # 1株配当 ÷ 株価で自前計算する
    dps = _num(info, "dividendRate", "trailingAnnualDividendRate")
    if dps:
        parts.append(f"配当利回り {dps / price * 100:.2f}%")
    roe = info.get("returnOnEquity")
    if isinstance(roe, (int, float)) and roe == roe:
        parts.append(f"ROE {roe * 100:.1f}%")
    return " 指標 " + " ／ ".join(parts) if parts else ""


def lot_line(price: float, stop: float | None, target: float | None = None,
             target_label: str = "利確目安") -> str:
    """売買単位（100株）あたりの必要資金と、損切り・目標に届いたときの損益。"""
    if not price:
        return ""
    lot = config.LOT_SIZE
    parts = [f"必要資金 {_man(price * lot)}"]
    if stop and stop < price:
        parts.append(f"損切りで −{_man((price - stop) * lot)}")
    if target and target > price:
        parts.append(f"{target_label}で +{_man((target - price) * lot)}")
    return f" {lot}株 " + " ／ ".join(parts)


def decision_lines(code4: str, df: pd.DataFrame, price: float, stop: float | None,
                   target: float | None = None, target_label: str = "利確目安",
                   idx_df: pd.DataFrame | None = None, info: dict | None = None) -> list[str]:
    """通知に足す判断数値の行（空行は除く）。info を渡さなければ fetch_valuation() で取る。"""
    if info is None:
        info = fetch_valuation(code4)
    lines = [pro_line(df, idx_df), valuation_line(info, price),
             lot_line(price, stop, target, target_label)]
    return [l for l in lines if l]
