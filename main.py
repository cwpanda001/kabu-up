"""TDnet監視 → キーワード判定 → チャート条件 → 通知

使い方:
  python main.py                       # 本番（GitHub Actions から cron 実行）
  python main.py --dry-run             # 通知を送らず標準出力へ
  python main.py --sample sample/tdnet_sample.html --dry-run --no-state
                                       # TDnet の代わりにサンプルHTMLで動作確認
  python main.py --force               # 休場日でも実行

状態は state/seen.json に持つ（開示IDごとに notified / pending / skipped）。
pending = 材料はポジティブだがチャート条件が未達。同日中（引け後開示は翌営業日中）は毎回再判定する。
"""
import argparse
import json
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import jpholiday

import config
from judge import judge
from notify import send
from screener import JST, screen
from tdnet import Disclosure, fetch_day, parse_list

STATE_PATH = "state/seen.json"
KEEP_DAYS = 3


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


def _yen(p: float) -> str:
    if p != p:  # NaN
        return "-"
    return f"{p:,.0f}" if p == int(p) else f"{p:,.1f}"


def fmt_hit(item: Disclosure, v: dict, s, today: str) -> str:
    label = "／".join(v["labels"])
    if v.get("ai"):
        label += f"（AI: {v['ai'].get('summary', '')}）"
    elif v.get("note"):
        label += f"（{v['note']}）"
    day = "" if item.date == today else f"{item.date[5:].replace('-', '/')} "
    return "\n".join([
        f"■ {item.code4} {item.name}",
        f" 開示 {day}{item.time}｜{item.title}",
        f" 判定 {label}",
        f" 株価 {_yen(s.price)}円（前日比 {s.gap_pct:+.1f}%）→ {s.entry_label}",
        f" 25MA {_yen(s.ma25)} ／ 75MA {_yen(s.ma75)} ／ RSI {s.rsi:.0f} ／ 出来高 {s.vol_ratio:.1f}倍",
        f" 損切り目安 {_yen(s.stop)}円（−{config.ATR_STOP_MULT:g}ATR）",
        f" {item.url}",
    ])


def run(items: list[Disclosure], now: datetime, state: dict, dry_run: bool) -> int:
    today = now.date().isoformat()
    hits, screened = [], 0
    # 前営業日分は 15:00 以降（引け後開示）のみ持ち越して再判定する
    targets = [it for it in items if it.date == today or it.time >= "15:00"]

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
        s = screen(it.code4, now)
        time.sleep(1.0)  # yfinance レート制限対策
        if s is None:
            print(f"[skip] {it.code4} {it.name}: 株価データ無し（ETF/REIT等）")
            state[it.id] = {"d": it.date, "s": "skipped"}
            continue

        if s.passed:
            print(f"[HIT]  {it.code4} {it.name}: {v['labels']} 出来高{s.vol_ratio:.1f}倍 gap{s.gap_pct:+.1f}%")
            hits.append((it, v, s))
            state[it.id] = {"d": it.date, "s": "notified", "v": v}
        else:
            print(f"[pend] {it.code4} {it.name}: {v['labels']} / {', '.join(s.reasons)}")
            state[it.id] = {"d": it.date, "s": "pending", "v": v}

    if hits:
        hits.sort(key=lambda h: (-h[1]["score"], -h[2].vol_ratio))
        body = [f"【材料×チャート一致】{now:%m/%d %H:%M}"]
        body += [fmt_hit(*h, today) for h in hits]
        if any(h[2].after_close for h in hits):
            body.append("※引け後の株価で判定。翌営業日の寄付きで再確認してから判断")
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
    args = ap.parse_args()

    now = datetime.now(JST)
    today = now.date()

    if args.test_notify:
        send(f"【tdnet-watch テスト通知】{now:%Y-%m-%d %H:%M}\n"
             "この通知が見えていれば通知先の設定は正常です。", dry_run=args.dry_run)
        print("テスト通知を送信した")
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
