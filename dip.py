"""急落検知。一時的な下げ（誤発注・パニック売り・一過性の悪材料）からの戻りを狙う枠。

  python main.py --dip-scan                    # 場中の15分おき実行に同居（watch.yml が常に付ける）
  python main.py --dip-scan --dry-run --force  # 手元で確認（休場日・夜間でも回す）

既存の「好材料×上昇トレンド」通知の裏返し。次の2系統を場中に拾って通知する。

  A. 【急落検知（悪材料なし）】 悪材料の開示が無いのに大きく下げた銘柄
     （誤発注・パニック売り・口座乗っ取りの投げ売りなど。対象は日経225＋state/universe.txt）
  B. 【一過性悪材料×急落】     不正アクセス・システム障害など一過性とみなせる開示が出て下げた銘柄
     （TDnet の開示から拾うので対象ユニバースの外でも検知する）

前提は「業績と関係ない下げは戻りやすい」だが、必ず戻る保証は無い（通知の末尾に注意書きを出す）。

ゲート（すべて満たすと通知。しきい値は config.DIP_*）:
  1. 前日終値比 ≤ −DIP_DROP_PCT。現在値で判定するので、届いた時点で戻り切っていれば鳴らない
  2. 日経平均の当日騰落率との差 ≤ −DIP_INDEX_DIFF_PT。全体が同じだけ下げた日は地合いの下げなので鳴らない
  3. 25MA > 75MA。中期トレンドが上向きのうちの押し目だけ拾い、下降トレンドの続きは拾わない
  4. 当日＋前営業日に本物の悪材料（DIP_NEGATIVE_KEYWORDS）の開示が無い
  5. 決算発表の当日・翌日でない（TDnet の決算短信、または yfinance の決算日が当日）
併記（合否に使わない）: 当日安値と戻り率・出来高倍率・RSI・ATR換算の下げ幅・週足の節目・決算接近

株価は既存どおり yfinance の日足（20分遅延）。当日の足に高値・安値・現在値・出来高が入るので、
分足を取らなくても「当日安値からの戻り率」は出せる。
"""
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import config
from chart_context import analyze, context_lines, earnings_note, market_condition, yen
from nikkei225 import load_universe
from notify import send
from screener import (bounce_pct, evaluate, fetch_history, fetch_history_batch, fetch_market,
                      next_earnings_date)
from stock_info import fetch_name

MAX_EXTRA = 10   # 開示キーワード経由でユニバースの外から足す銘柄数の上限（レート制限対策）


@dataclass
class Dip:
    ok: bool
    reasons: list = field(default_factory=list)   # 不合格理由
    price: float = 0.0
    prev_close: float = 0.0
    gap_pct: float = 0.0          # 前日終値比%（現在値）
    low: float = 0.0              # 当日安値
    low_pct: float = 0.0          # 当日安値の前日終値比%
    bounce: float | None = None   # 当日安値からの戻り率%（None = 前日終値を割っていない）
    idx_gap: float | None = None  # 日経平均の当日騰落率%
    diff_pt: float | None = None  # 銘柄 − 指数（pt）
    ma25: float = 0.0
    ma75: float = 0.0
    rsi: float = 0.0
    vol_ratio: float = 0.0
    atr: float = 0.0
    atr_mult: float = 0.0         # 下げ幅 ÷ ATR14（普段の値動きの何倍か）
    after_close: bool = False


def index_gap(df, today: date) -> float | None:
    """指数の当日騰落率%。当日の足が無ければ None（寄付き前・休場日・データ遅延）。"""
    if df is None:
        return None
    d = df.dropna(subset=["Close"])
    if len(d) < 2 or d.index[-1].date() != today:
        return None
    return (float(d["Close"].iloc[-1]) / float(d["Close"].iloc[-2]) - 1) * 100


def evaluate_dip(df, now: datetime, idx_gap: float | None) -> Dip:
    """ネットワーク不要の値動きゲート（1〜3）。開示・決算のゲート（4〜5）は dip_scan 側で見る。"""
    s = evaluate(df, now, require_volume=False, require_above_ma25=False)
    if not s.price:
        return Dip(False, list(s.reasons))
    d = Dip(True, price=s.price, prev_close=s.prev_close, gap_pct=s.gap_pct,
            ma25=s.ma25, ma75=s.ma75, rsi=s.rsi, vol_ratio=s.vol_ratio, atr=s.atr,
            after_close=s.after_close, idx_gap=idx_gap, low=s.day_low)
    if d.low and d.prev_close:
        d.low_pct = (d.low / d.prev_close - 1) * 100
        d.bounce = bounce_pct(d.price, d.prev_close, d.low)
    if d.atr > 0:
        d.atr_mult = (d.prev_close - d.price) / d.atr

    if "当日株価未取得" in s.reasons:
        d.reasons.append("当日株価未取得")
    if d.gap_pct > -config.DIP_DROP_PCT:
        d.reasons.append(f"下落{d.gap_pct:+.1f}%（−{config.DIP_DROP_PCT:g}%未満）")
    if idx_gap is None:
        d.reasons.append("日経平均の当日値不明")
    else:
        d.diff_pt = d.gap_pct - idx_gap
        if d.diff_pt > -config.DIP_INDEX_DIFF_PT:
            d.reasons.append(f"指数差{d.diff_pt:+.1f}pt（地合いの下げ）")
    if not d.ma25 > d.ma75:
        d.reasons.append("トレンド不成立(25MA>75MA)")
    d.ok = not d.reasons
    return d


def _hits(title: str, words: list[str]) -> list[str]:
    return [w for w in words if w in title]


def classify_disclosures(items: list) -> tuple[str, list[str]]:
    """その銘柄の当日＋前営業日の開示から急落の扱いを決める。(種別, ヒットした語)。

      negative  : 本物の悪材料あり → 見送り
      earnings  : 決算発表あり     → 見送り（決算の下げは一時的ではない）
      transient : 一過性の悪材料   → 【一過性悪材料×急落】
      none      : 該当なし         → 【急落検知（悪材料なし）】
    一過性の材料と本物の悪材料が同時に出ていれば negative を優先する（保守的）。
    """
    neg, earn, trans = [], [], []
    for it in items:
        neg += _hits(it.title, config.DIP_NEGATIVE_KEYWORDS)
        earn += _hits(it.title, config.DIP_EARNINGS_KEYWORDS)
        trans += _hits(it.title, config.DIP_TRANSIENT_KEYWORDS)

    def uniq(xs):
        out = []
        for x in xs:
            if x not in out:
                out.append(x)
        return out

    if neg:
        return "negative", uniq(neg)
    if earn:
        return "earnings", uniq(earn)
    if trans:
        return "transient", uniq(trans)
    return "none", []


def _disc_lines(items: list, today_iso: str, limit: int = 3) -> list[str]:
    out = []
    for it in items[:limit]:
        day = "" if it.date == today_iso else f"{it.date[5:].replace('-', '/')} "
        out.append(f" 開示 {day}{it.time}｜{it.title}")
    if len(items) > limit:
        out.append(f" ほか開示 {len(items) - limit} 件")
    return out


def fmt_dip(code4: str, name: str, d: Dip, ctx, earn: str, items: list,
            kind: str, labels: list[str], today_iso: str) -> str:
    """急落1銘柄分の本文。kind は classify_disclosures の種別（transient / none）。"""
    lines = [f"■ {code4} {name}".rstrip()]
    lines += _disc_lines(items, today_iso)
    if kind == "transient":
        lines.append(f" 判定 一過性の悪材料（{'／'.join(labels)}）× 急落")
    elif items:
        lines.append(" 判定 悪材料なしの急落（開示はあるが悪材料・決算ではない）")
    else:
        lines.append(" 判定 悪材料なしの急落（当日＋前営業日に開示なし）")

    idx = (f"日経平均 {d.idx_gap:+.1f}%・差 {d.diff_pt:+.1f}pt"
           if d.idx_gap is not None and d.diff_pt is not None else "日経平均 不明")
    low = f"当日安値 {yen(d.low)}円（{d.low_pct:+.1f}%）" if d.low else "当日安値 不明"
    if d.bounce is None:
        bounce = "戻り率 －"
    elif d.bounce < config.DIP_BOUNCE_CONFIRM_PCT:
        bounce = f"戻り率 {d.bounce:.0f}%（安値圏・反発未確認）"
    else:
        bounce = f"戻り率 {d.bounce:.0f}%"
    lines.append(f" 下落 前日比 {d.gap_pct:+.1f}%（{idx}）／ {low} → {bounce}")
    lines.append(f" 株価 {yen(d.price)}円（前日終値 {yen(d.prev_close)}円）")
    lines.append(f" 25MA {yen(d.ma25)} ／ 75MA {yen(d.ma75)} ／ RSI {d.rsi:.0f} ／ "
                 f"出来高 {d.vol_ratio:.1f}倍 ／ 下げ幅 {d.atr_mult:.1f}ATR")
    lines += context_lines(ctx)
    if earn:
        lines.append(f" {earn}")

    # 損切りは当日安値割れ、戻り目標は前日終値。反発が確認できるまではリスクリワードを出さない
    # （安値に張り付いた状態で計算すると損切り幅がほぼ0になり、比が意味を持たないため）
    up = f"戻り目標 前日終値 {yen(d.prev_close)}円（+{(d.prev_close / d.price - 1) * 100:.1f}%）"
    if d.low and d.bounce is not None and d.bounce >= config.DIP_BOUNCE_CONFIRM_PCT and d.price > d.low:
        risk_pct = (d.price - d.low) / d.price * 100
        rr = (d.prev_close - d.price) / (d.price - d.low)
        lines.append(f" 損切り目安 {yen(d.low)}円（当日安値割れ・−{risk_pct:.1f}%）／ {up} ／ リスクリワード {rr:.1f}")
    elif d.low:
        lines.append(f" 損切り目安 {yen(d.low)}円（当日安値割れ）／ {up} ／ 反発未確認のためリスクリワード算出不可")
    if kind == "transient":
        lines += [f" {it.url}" for it in items if it.url]
    return "\n".join(l for l in lines if l)


def _by_code(items: list) -> dict[str, list]:
    out: dict[str, list] = {}
    for it in items:
        out.setdefault(it.code4, []).append(it)
    return out


HEAD = {"transient": "【一過性悪材料×急落】", "none": "【急落検知（悪材料なし）】"}


def _footer(kind: str) -> str:
    if kind == "transient":
        return ("※不正アクセス・システム障害など一過性とみなせる悪材料での下げ。業績への影響の有無はPDFで確認。\n"
                "　必ず戻る保証は無い。当日安値割れで撤退する前提で判断する")
    return (f"※前日終値比 −{config.DIP_DROP_PCT:g}% 以上・日経平均との差 −{config.DIP_INDEX_DIFF_PT:g}pt 以上・"
            "25MA>75MA・悪材料と決算の開示なし の銘柄。\n"
            "　下げの理由（ニュース・SNS）は各自で確認。必ず戻る保証は無い。当日安値割れで撤退する前提で判断する")


def dip_scan(now: datetime, items: list, state: dict, dry_run: bool, frames: dict | None = None) -> dict:
    """急落検知の本体。items は当日＋前営業日の TDnet 開示（run() と同じもの）。

    frames を渡すと（日次実行で教材スキャンが取った日足）それを使い回し、
    ユニバースの日足を取り直さない。
    """
    today = now.date()
    today_iso = today.isoformat()
    mkt_df = fetch_market()
    mkt = market_condition(mkt_df)
    ig = index_gap(mkt_df, today)
    stats = {"scanned": 0, "nodata": 0, "hits": 0, "idx": ig}
    if ig is None:
        print("[dip] 日経平均の当日値が無いため急落検知をスキップ（寄付き前・休場日・指数データ遅延）")
        return stats

    by_code = _by_code(items)
    codes = load_universe() if config.DIP_SCAN_UNIVERSE else []
    extra = [c for c, its in by_code.items()
             if c not in codes and classify_disclosures(its)[0] == "transient"][:MAX_EXTRA]
    targets = codes + extra
    stats["scanned"] = len(targets)
    print(f"急落検知 対象{len(targets)}銘柄（開示キーワード経由 {len(extra)}）"
          f"/ 日経平均 {ig:+.1f}% / 地合い {mkt[0] or '判定不能'}")

    if frames is None:
        frames = fetch_history_batch(codes, period=config.DIP_HISTORY_PERIOD) if codes else {}
    else:
        frames = dict(frames)
    for c in extra:
        frames[c] = fetch_history(c, period=config.DIP_HISTORY_PERIOD)
        time.sleep(1.0)  # yfinance レート制限対策

    cooldown = (today - timedelta(days=config.DIP_COOLDOWN_DAYS)).isoformat()
    groups: dict[str, list] = {"transient": [], "none": []}
    for code4 in targets:
        df = frames.get(code4)
        if df is None:
            stats["nodata"] += 1
            continue
        d = evaluate_dip(df, now, ig)
        if not d.ok:
            continue
        prev = state.get(f"dip:{code4}")
        if prev and prev.get("d", "") > cooldown:
            print(f"[cool] {code4}: {prev['d']} に急落通知済みのため見送り")
            continue
        mine = by_code.get(code4, [])
        kind, labels = classify_disclosures(mine)
        if kind in ("negative", "earnings"):
            why = "悪材料" if kind == "negative" else "決算"
            print(f"[dip-skip] {code4}: {d.gap_pct:+.1f}% {why}の開示あり（{'／'.join(labels)}）")
            continue
        edate = next_earnings_date(code4)
        if edate == today:
            print(f"[dip-skip] {code4}: {d.gap_pct:+.1f}% 決算発表当日")
            continue
        ctx = analyze(df)
        earn = earnings_note(edate, today)
        bounce = "－" if d.bounce is None else f"{d.bounce:.0f}%"
        print(f"[DIP]  {code4}: {d.gap_pct:+.1f}%（指数差{d.diff_pt:+.1f}pt）"
              f"安値{d.low_pct:+.1f}% 戻り{bounce} {kind}")
        block = fmt_dip(code4, fetch_name(code4), d, ctx, earn, mine, kind, labels, today_iso)
        groups[kind].append((d, block))
        state[f"dip:{code4}"] = {"d": today_iso, "s": "dip"}

    if stats["nodata"]:
        print(f"[dip] 株価データ無し {stats['nodata']} 銘柄")
    for kind in ("transient", "none"):
        hs = groups[kind]
        if not hs:
            continue
        hs.sort(key=lambda h: h[0].gap_pct)   # 下げの大きい順
        head = f"{HEAD[kind]}{now:%m/%d %H:%M}"
        head += (f"\n地合い 日経平均 {mkt[0]}（{mkt[1]}）／ 本日 {ig:+.1f}%" if mkt[0]
                 else f"\n日経平均 本日 {ig:+.1f}%")
        body = [head] + [b for _, b in hs] + [_footer(kind)]
        if any(h[0].after_close for h in hs):
            body.append("※引け後の株価（終値）で判定。翌営業日の寄付きで戻っていることもあるのでギャップを再確認")
        send("\n\n".join(body), dry_run=dry_run)
    stats["hits"] = sum(len(v) for v in groups.values())
    return stats
