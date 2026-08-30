"""銘柄コードを指定して、その銘柄の現在状況をレポートする。

  python main.py --stock 7203           # 1銘柄
  python main.py --stock 7203,6758      # カンマ区切りで複数

screener と同じ条件でチャート指標を判定し、当日＋前営業日に適時開示が
あればタイトルを添える。結果は notify.send() で登録済みの通知先へ送る。
"""
import re
import time
from datetime import date, datetime

import config
from judge import keyword_judge
from screener import JST, evaluate, fetch_history
from tdnet import fetch_day


def yen(p: float) -> str:
    if p != p:  # NaN
        return "-"
    return f"{p:,.0f}" if p == int(p) else f"{p:,.1f}"


def normalize_code(raw: str) -> str | None:
    """入力を4桁の銘柄コードに整える。TDnet式5桁（末尾0）も受ける。不正なら None。"""
    c = raw.strip().upper()
    if len(c) == 5 and c.endswith("0"):
        c = c[:4]
    return c if re.fullmatch(r"\d[0-9A-Z]{3}", c) else None


def parse_codes(arg: str) -> list[str]:
    """カンマ・読点・空白区切りの入力を分解する（空要素と重複は除く）。"""
    out = []
    for tok in re.split(r"[,、\s]+", arg):
        if tok and tok not in out:
            out.append(tok)
    return out


def fetch_name(code4: str) -> str:
    """yfinance から社名を取る（英語名）。失敗しても空文字で続行する。"""
    try:
        import yfinance as yf
        info = yf.Ticker(f"{code4}.T").info or {}
        return info.get("longName") or info.get("shortName") or ""
    except Exception:
        return ""


def _disc_lines(code4: str, items: list | None, today_iso: str) -> list[str]:
    """その銘柄の適時開示行。items=None は開示を照会していない（行を出さない）。"""
    if items is None:
        return []
    mine = [it for it in items if it.code4 == code4]
    if not mine:
        return [" 開示 なし（当日＋前営業日）"]
    out = []
    for it in mine[:5]:
        v = keyword_judge(it.title)
        tag = f"〔{'／'.join(v['labels'])}〕" if v and v["labels"] else ""
        day = "" if it.date == today_iso else f"{it.date[5:].replace('-', '/')} "
        out.append(f" 開示 {day}{it.time}｜{it.title}{tag}")
    if len(mine) > 5:
        out.append(f" ほか開示 {len(mine) - 5} 件")
    return out


def fmt_stock(code4: str, name: str, s, data_date: date | None = None,
              today: date | None = None, disc_lines: list[str] | None = None) -> str:
    """1銘柄分のレポート。s は screener.Screen（None = 株価データ無し）。"""
    lines = [f"■ {code4} {name}".rstrip()]
    lines += disc_lines or []
    if s is None:
        lines.append(" 株価データ無し（コード誤り、またはETF・REIT等の可能性）")
        return "\n".join(lines)
    if not s.price:
        lines.append(f" 判定不能（{'、'.join(s.reasons)}）")
        return "\n".join(lines)
    is_today = data_date == today
    note = "" if is_today or data_date is None else f"（{data_date:%m/%d}終値）"
    lines += [
        f" 株価 {yen(s.price)}円{note}（前日比 {s.gap_pct:+.1f}%）",
        f" トレンド {'上昇（現在値>25MA>75MA）' if s.price > s.ma25 > s.ma75 else '上昇トレンド不成立'}",
        f" 25MA {yen(s.ma25)} ／ 75MA {yen(s.ma75)} ／ RSI {s.rsi:.0f} ／ 出来高 "
        + (f"{s.vol_ratio:.1f}倍" if is_today else "-"),
        f" 損切り目安 {yen(s.stop)}円（−{config.ATR_STOP_MULT:g}ATR）",
        f" チャート条件 {'合格 → ' + s.entry_label if s.passed else '未達（' + '、'.join(s.reasons) + '）'}",
    ]
    return "\n".join(lines)


def stock_report(arg: str, now: datetime | None = None,
                 disclosure_days: list[date] | None = None) -> str:
    """コード指定（カンマ区切り可）の状況レポート本文を組み立てる。"""
    now = now or datetime.now(JST)
    today = now.date()
    items = None
    if disclosure_days:
        items = [it for d in disclosure_days for it in fetch_day(d)]

    codes = parse_codes(arg)
    dropped = len(codes) > config.MAX_SCREEN_PER_RUN
    blocks, seen = [], set()
    for raw in codes[: config.MAX_SCREEN_PER_RUN]:
        code4 = normalize_code(raw)
        if code4 is None:
            blocks.append(f"■ {raw}\n 無効なコード（例: 7203 のように4桁で指定）")
            continue
        if code4 in seen:
            continue
        if seen:
            time.sleep(1.0)  # yfinance レート制限対策
        seen.add(code4)
        disc = _disc_lines(code4, items, today.isoformat())
        df = fetch_history(code4)
        if df is None:
            blocks.append(fmt_stock(code4, "", None, disc_lines=disc))
            continue
        closes = df.dropna(subset=["Close"])
        data_date = closes.index[-1].date() if len(closes) else None
        blocks.append(fmt_stock(code4, fetch_name(code4), evaluate(df, now),
                                data_date, today, disc))
    if not blocks:
        blocks.append("銘柄コードが指定されていない（例: --stock 7203）")
    if dropped:
        blocks.append(f"※一度に照会できるのは {config.MAX_SCREEN_PER_RUN} 銘柄まで。超過分は省略した")
    return "\n\n".join([f"【銘柄状況】{now:%m/%d %H:%M}"] + blocks
                       + ["※判断材料であり売買の推奨ではない"])
