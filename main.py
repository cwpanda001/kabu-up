"""TDnet監視 → キーワード判定 → チャート条件 → 教材判定を併記して通知

使い方:
  python main.py                       # 本番（GitHub Actions から cron 実行）
  python main.py --dry-run             # 通知を送らず標準出力へ
  python main.py --sample sample/tdnet_sample.html --dry-run --no-state
                                       # TDnet の代わりにサンプルHTMLで動作確認
  python main.py --force               # 休場日でも実行
  python main.py --stock 7203          # 銘柄コードを指定して現在状況を通知

状態は state/seen.json に持つ（開示IDごとに notified / pending / skipped）。
pending = 材料はポジティブだがチャート条件が未達。同日中（引け後開示は翌営業日中）は毎回再判定する。
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta

import config
from chart_context import (analyze, context_lines, earnings_note, is_trading_day,
                           market_condition, prev_trading_day, stance, yen)
from judge import judge
from notify import send
from screener import JST, evaluate, fetch_history, fetch_market, next_earnings_date
from stock_info import stock_report
from tdnet import Disclosure, fetch_day, parse_list

STATE_PATH = "state/seen.json"
KEEP_DAYS = 3


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict, today: date) -> None:
    cutoff = (today - timedelta(days=KEEP_DAYS)).isoformat()
    state = {k: v for k, v in state.items() if v.get("d", "") >= cutoff}
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0)


def fmt_hit(item: Disclosure, v: dict, s, ctx, earn: str, mkt_label: str, today: str) -> str:
    label = "／".join(v["labels"])
    if v.get("ai"):
        label += f"（AI: {v['ai'].get('summary', '')}）"
    elif v.get("note"):
        label += f"（{v['note']}）"
    day = "" if item.date == today else f"{item.date[5:].replace('-', '/')} "
    lines = [
        f"■ {item.code4} {item.name}",
        f" 開示 {day}{item.time}｜{item.title}",
        f" 判定 {label}",
        f" 株価 {yen(s.price)}円（前日比 {s.gap_pct:+.1f}%）→ {s.entry_label}",
        f" 25MA {yen(s.ma25)} ／ 75MA {yen(s.ma75)} ／ RSI {s.rsi:.0f} ／ 出来高 {s.vol_ratio:.1f}倍",
    ]
    lines += context_lines(ctx)
    if earn:
        lines.append(f" {earn}")
    lines += [
        f" 総合 {stance(ctx, mkt_label)}",
        f" 損切り目安 {yen(s.stop)}円（−{config.ATR_STOP_MULT:g}ATR）",
        f" {item.url}",
    ]
    return "\n".join(lines)


def run(items: list[Disclosure], now: datetime, state: dict, dry_run: bool) -> int:
    today = now.date().isoformat()
    hits, screened = [], 0
    # 前営業日分は 15:00 以降（引け後開示）のみ持ち越して再判定する
    targets = [it for it in items if it.date == today or it.time >= "15:00"]

    mkt = ("", "")
    if targets:
        mkt = market_condition(fetch_market())
        if mkt[0]:
            print(f"地合い 日経平均 {mkt[0]}（{mkt[1]}）")

    for it in targets:
        st = state.get(it.id)
        if st and st.get("s") in ("notified", "skipped"):
            continue

        v = st["v"] if st else judge(it)
        if v is None:
            state[it.id] = {"d": it.date, "s": "skipped"}
            continue

        if screened >= config.MAX_SCREEN_PER_RUN:
            state[it.id] = {"d": it.date, "s": "pending", "v": v}
            continue
        screened += 1
        df = fetch_history(it.code4)
        time.sleep(1.0)  # yfinance レート制限対策
        if df is None:
            print(f"[skip] {it.code4} {it.name}: 株価データ無し（ETF/REIT等）")
            state[it.id] = {"d": it.date, "s": "skipped"}
            continue
        s = evaluate(df, now)

        if not s.passed:
            print(f"[pend] {it.code4} {it.name}: {v['labels']} / {', '.join(s.reasons)}")
            state[it.id] = {"d": it.date, "s": "pending", "v": v}
            continue
        if config.MARKET_FILTER_HARD and mkt[0] == "悪化":
            print(f"[hold] {it.code4} {it.name}: 地合い悪化のため通知保留（{mkt[1]}）")
            state[it.id] = {"d": it.date, "s": "pending", "v": v}
            continue

        ctx = analyze(df)
        earn = earnings_note(next_earnings_date(it.code4), now.date())
        print(f"[HIT]  {it.code4} {it.name}: {v['labels']} 出来高{s.vol_ratio:.1f}倍 "
              f"gap{s.gap_pct:+.1f}% {ctx.stage}")
        hits.append((it, v, s, ctx, earn))
        state[it.id] = {"d": it.date, "s": "notified", "v": v}

    if hits:
        hits.sort(key=lambda h: (-h[1]["score"], -h[2].vol_ratio))
        head = f"【材料×チャート一致】{now:%m/%d %H:%M}"
        if mkt[0]:
            head += f"\n地合い 日経平均 {mkt[0]}（{mkt[1]}）"
        body = [head]
        body += [fmt_hit(it, v, s, ctx, earn, mkt[0], today) for it, v, s, ctx, earn in hits]
        if any(h[2].after_close for h in hits):
            body.append("※引け後の株価（終値）で判定。急ぐ場合はPTS（夜間取引）の板を確認して対応可。\n"
                        "　翌営業日に持ち越す場合は寄付きのギャップを再確認してから判断")
        send("\n\n".join(body), dry_run=dry_run)
    return len(hits)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", help="TDnet の代わりに読むHTMLファイル")
    ap.add_argument("--dry-run", action="store_true", help="通知を送らず標準出力へ")
    ap.add_argument("--no-state", action="store_true", help="state/seen.json を読み書きしない")
    ap.add_argument("--force", action="store_true", help="休場日でも実行")
    ap.add_argument("--test-notify", action="store_true",
                    help="テスト通知を1件送って終了（通知先の設定確認用）")
    ap.add_argument("--stock", metavar="CODES",
                    help="銘柄コードを指定して現在状況を通知して終了（カンマ区切りで複数可。例: 7203,6758）")
    args = ap.parse_args()

    now = datetime.now(JST)
    today = now.date()

    if args.test_notify:
        send(f"【tdnet-watch テスト通知】{now:%Y-%m-%d %H:%M}\n"
             "この通知が見えていれば通知先の設定は正常です。", dry_run=args.dry_run)
        print("テスト通知を送信した")
        return
    if args.stock:
        # 休場日でも動く（直近営業日の終値で表示される）
        send(stock_report(args.stock, now,
                          disclosure_days=[today, prev_trading_day(today)]),
             dry_run=args.dry_run)
        print(f"銘柄状況を送信した: {args.stock}")
        return
    if not args.sample and not args.force and not is_trading_day(today):
        print(f"{today} は休場日。終了")
        return

    if args.sample:
        with open(args.sample, encoding="utf-8") as f:
            items = parse_list(f.read(), today)
    else:
        items = fetch_day(today) + fetch_day(prev_trading_day(today))
    print(f"{now:%Y-%m-%d %H:%M} 開示 {len(items)} 件取得")

    state = {} if args.no_state else load_state()
    n = run(items, now, state, args.dry_run)
    if not args.no_state:
        save_state(state, today)
    print(f"通知 {n} 件 / 状態 {len(state)} 件")


if __name__ == "__main__":
    main()
