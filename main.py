"""TDnet監視 → キーワード判定 → チャート条件 → 教材判定を併記して通知

使い方:
  python main.py                       # 本番（GitHub Actions から cron 実行）
  python main.py --dry-run             # 通知を送らず標準出力へ
  python main.py --sample sample/tdnet_sample.html --dry-run --no-state
                                       # TDnet の代わりにサンプルHTMLで動作確認
  python main.py --force               # 休場日でも実行
  python main.py --stock 7203          # 銘柄コードを指定して現在状況を通知
  python main.py --scan-market         # 材料ニュース無しでも教材条件が満点の銘柄を通知
  python main.py --chart-scan          # 材料ニュース無しでも「もともとのチャート4条件」が揃った銘柄を通知（場中の毎回実行に同居）
  python main.py --dip-scan            # 悪材料なし／一過性悪材料の急落を通知（場中の毎回実行に同居）
  python main.py --summary             # その実行の結果を日次サマリとして通知

「株が上がりそう」と言える条件の組み合わせは4つあり、どれか1つが揃えば通知する:
  1. 材料×チャート  : 好材料の開示 × もともとのチャート4条件（run()）
  2. 材料×教材条件  : 好材料の開示 × 教材条件（チャート4条件が未達でも通知。run()）
  3. 教材条件のみ    : 材料ニュース無し × 教材条件（scan_market。引け後の日次）
  4. チャート条件のみ: 材料ニュース無し × もともとのチャート4条件（chart_scan。場中）

状態は state/seen.json に持つ（開示IDごとに notified / pending / skipped）。
pending = 材料はポジティブだがチャート条件も教材条件も未達。同日中（引け後開示は翌営業日中）は毎回再判定する。
mkt:銘柄コード   = 教材スキャンで通知済み（SCAN_COOLDOWN_DAYS のあいだ再通知しない）。
chart:銘柄コード = チャート条件スキャンで通知済み（CHART_COOLDOWN_DAYS のあいだ再通知しない）。
dip:銘柄コード   = 急落検知で通知済み（DIP_COOLDOWN_DAYS のあいだ再通知しない）。
"""
import argparse
import json
import os
import time
from datetime import date, datetime
from datetime import time as dtime

import config
import names
from chart_context import (analyze, context_lines, earnings_note, is_trading_day,
                           market_condition, prev_trading_day, room_line, scan_ok, stance,
                           trading_days_ago, yen)
from dip import dip_scan
from judge import judge
from nikkei225 import load_universe
from notify import send
from screener import (JST, evaluate, fetch_history, fetch_history_batch, fetch_market,
                      next_earnings_date)
from stock_info import fetch_name, stock_report
from tdnet import Disclosure, fetch_day, parse_list

STATE_PATH = "state/seen.json"
# 教材スキャン・急落検知のクールダウン判定に state を使うので、保持期間はそれより短くしない。
# 営業日で数える（暦日だと3連休明けに前営業日の「通知済み」まで消えて、引け後開示を
# 実行のたびに再通知していた）
KEEP_DAYS = max(3, config.SCAN_COOLDOWN_DAYS, config.DIP_COOLDOWN_DAYS, config.CHART_COOLDOWN_DAYS)
# 急落検知・チャート条件スキャンは場中の枠。これ以降の実行では回さない
# （引けの判定は 16:35 の日次実行が --scan-market と一緒に行う）
DIP_SCAN_UNTIL = dtime(16, 0)
# 材料ニュース以外の枠（教材条件／チャート条件／急落）が state に残す記録の種別。日次サマリの材料側の集計から除く
_SCAN_STATUSES = ("market", "chart", "dip")


def load_state() -> dict:
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def prune_state(state: dict, today: date) -> dict:
    """KEEP_DAYS 営業日より古い記録を落とす。

    run() は前営業日の引け後開示も判定対象にするので、前営業日の記録は必ず残す
    （KEEP_DAYS>=1 なので営業日で数えれば自動的に残る）。
    """
    cutoff = trading_days_ago(today, KEEP_DAYS).isoformat()
    return {k: v for k, v in state.items() if v.get("d", "") >= cutoff}


def save_state(state: dict, today: date) -> None:
    state = prune_state(state, today)
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=0)


def merge_judge(vs: list[dict]) -> dict:
    """同一銘柄の複数開示の判定をまとめる（ラベルは重複を除いて連結、スコアは最大）。"""
    top = max(vs, key=lambda v: v["score"])
    labels: list[str] = []
    for v in vs:
        for l in v["labels"]:
            if l not in labels:
                labels.append(l)
    return {**top, "labels": labels}


def mark_state(state: dict, group: list[tuple], status: str,
               code4: str = "", name: str = "", reasons: str = "") -> None:
    """同じ銘柄の該当開示すべてに同じ判定結果を記録する。

    銘柄コードは通知済みでも残す。「材料ニュースなし」の枠（教材条件スキャン・
    チャート条件スキャン）が、材料で扱っている銘柄を重ねて通知しないようにするため。
    """
    for it, jv in group:
        e = {"d": it.date, "s": status, "v": jv}
        if code4:
            e["c"] = code4
        if status == "pending":
            e.update({"n": name, "r": reasons})
        state[it.id] = e


def news_codes(state: dict, today: date) -> set[str]:
    """材料ニュースの枠で扱っている銘柄（当日＋前営業日の開示で通知済み・保留中）。

    「材料ニュースなし」を名乗る枠はこれを除いて判定する。同じ銘柄が材料の通知と
    スキャンの通知に二重で並ぶのを防ぐ。
    """
    since = prev_trading_day(today).isoformat()
    return {v["c"] for v in state.values()
            if v.get("c") and v.get("s") in ("notified", "pending") and v.get("d", "") >= since}


def lazy_frames(period: str | None = None):
    """ユニバースの日足を「最初に必要になったとき」に1回だけ取る呼び出し可能オブジェクト。

    場中はチャート条件スキャンと急落検知が同じ日足を使うので、実行ごとに1回で済ませる。
    急落検知が指数の当日値なしでスキップする実行では、呼ばれなければ取りに行かない。
    """
    cache: dict = {}

    def get() -> dict:
        if "frames" not in cache:
            cache["frames"] = fetch_history_batch(load_universe(), period=period)
        return cache["frames"]
    return get


def _chart_lines(s) -> list[str]:
    """株価・指標の行（材料ヒットと教材スキャンで共通）。"""
    return [
        f" 株価 {yen(s.price)}円（前日比 {s.gap_pct:+.1f}%）→ {s.entry_label}",
        f" 25MA {yen(s.ma25)} ／ 75MA {yen(s.ma75)} ／ RSI {s.rsi:.0f} ／ 出来高 {s.vol_ratio:.1f}倍",
    ]


def fmt_hit(items: list[Disclosure], v: dict, s, ctx, earn: str,
            mkt_label: str, today: str, chart_note: str = "") -> str:
    """材料ヒット1銘柄分。items は同じ銘柄の該当開示（複数ありうる）。

    chart_note が空でなければ「材料×教材条件」の通知（もともとのチャート4条件は
    未達だが教材条件が揃った）で、未達だった条件を判定行に併記する。
    """
    label = "／".join(v["labels"])
    if v.get("ai"):
        label += f"（AI: {v['ai'].get('summary', '')}）"
    elif v.get("note"):
        label += f"（{v['note']}）"
    lines = [f"■ {items[0].code4} {names.display(items[0].code4, items[0].name)}"]
    for it in items:
        day = "" if it.date == today else f"{it.date[5:].replace('-', '/')} "
        lines.append(f" 開示 {day}{it.time}｜{it.title}")
    lines.append(f" 判定 {label}")
    if chart_note:
        lines.append(f" 条件 教材条件クリア（追随期の押し目反発）／ チャート条件は未達: {chart_note}")
    lines += _chart_lines(s)
    lines += context_lines(ctx)
    lines.append(room_line(ctx, s.price, s.stop))
    if earn:
        lines.append(f" {earn}")
    lines += [
        f" 総合 {stance(ctx, mkt_label)}",
        f" 損切り目安 {yen(s.stop)}円（−{config.ATR_STOP_MULT:g}ATR）",
    ]
    lines += [f" {it.url}" for it in items if it.url]
    return "\n".join(l for l in lines if l)


def fmt_scan_hit(code4: str, name: str, s, ctx, earn: str, mkt_label: str,
                 verdict: str) -> str:
    """スキャン（教材条件／チャート条件）のヒット1銘柄分（材料ニュースは無い）。"""
    lines = [f"■ {code4} {name}".rstrip(), f" 判定 {verdict}"]
    lines += _chart_lines(s)
    lines += context_lines(ctx)
    lines.append(room_line(ctx, s.price, s.stop))
    if earn:
        lines.append(f" {earn}")
    lines += [
        f" 総合 {stance(ctx, mkt_label)}",
        f" 損切り目安 {yen(s.stop)}円（−{config.ATR_STOP_MULT:g}ATR）",
    ]
    return "\n".join(l for l in lines if l)


def run(items: list[Disclosure], now: datetime, state: dict, dry_run: bool) -> dict:
    """TDnet の材料判定 → チャート条件 → 通知。集計値を返す。"""
    today = now.date().isoformat()
    # 前営業日分は 15:00 以降（引け後開示）のみ持ち越して再判定する
    targets = [it for it in items if it.date == today or it.time >= "15:00"]

    mkt = ("", "")
    if targets:
        mkt = market_condition(fetch_market())
        if mkt[0]:
            print(f"地合い 日経平均 {mkt[0]}（{mkt[1]}）")

    # 判定対象を銘柄コード単位にまとめる。1社が複数の該当開示を出しても
    # 株価取得と通知は1回にする（同じ銘柄が通知に二重で並ぶのを防ぐ）。
    todo: dict[str, list[tuple[Disclosure, dict]]] = {}
    for it in targets:
        st = state.get(it.id)
        if st and st.get("s") in ("notified", "skipped"):
            continue
        v = st["v"] if st else judge(it)
        if v is None:
            state[it.id] = {"d": it.date, "s": "skipped"}
            continue
        todo.setdefault(it.code4, []).append((it, v))

    hits, screened = [], 0
    for code4, group in todo.items():
        group.sort(key=lambda g: -g[1]["score"])   # 代表はスコアの高い開示
        its = [g[0] for g in group]
        v = merge_judge([g[1] for g in group])
        name = names.display(code4, its[0].name)   # 日本語のフル社名（無ければTDnetの略称）

        def mark(status: str, reasons: str = "") -> None:
            mark_state(state, group, status, code4, name, reasons)

        if screened >= config.MAX_SCREEN_PER_RUN:
            mark("pending", f"スクリーニング上限{config.MAX_SCREEN_PER_RUN}銘柄で未判定")
            continue
        screened += 1
        df = fetch_history(code4)
        time.sleep(1.0)  # yfinance レート制限対策
        if df is None:
            print(f"[skip] {code4} {name}: 株価データ無し（ETF/REIT等）")
            mark("skipped")
            continue
        s = evaluate(df, now)
        ctx = analyze(df)
        chart_note = ""

        if not s.passed:
            # もともとのチャート4条件が未達でも、教材条件（上昇トレンド・追随期・25MA上向き・
            # 地合い良好）が揃っていれば「材料×教材条件」として通知する。押し目からの反発は
            # 定義上いったん 25MA を割り、出来高も細るので、教材スキャンと同じ緩いガード
            # （25MA>75MA・RSI・ギャップ）で見る。どちらも未達なら従来どおり保留して再判定
            soft = evaluate(df, now, require_volume=config.SCAN_REQUIRE_VOLUME,
                            require_above_ma25=config.SCAN_REQUIRE_ABOVE_MA25)
            if soft.passed and scan_ok(ctx, mkt[0]):
                chart_note, s = ", ".join(s.reasons), soft
            else:
                reasons = ", ".join(s.reasons)
                print(f"[pend] {code4} {name}: {v['labels']} / {reasons}")
                mark("pending", reasons)
                continue
        if config.MARKET_FILTER_HARD and mkt[0] == "悪化":
            print(f"[hold] {code4} {name}: 地合い悪化のため通知保留（{mkt[1]}）")
            mark("pending", f"地合い悪化のため保留（{mkt[1]}）")
            continue

        earn = earnings_note(next_earnings_date(code4), now.date())
        tag = "HIT" if not chart_note else "HIT*"
        print(f"[{tag}]  {code4} {name}: {v['labels']} 出来高{s.vol_ratio:.1f}倍 "
              f"gap{s.gap_pct:+.1f}% {ctx.stage}" + (f"（教材条件で通知: {chart_note}）" if chart_note else ""))
        hits.append((its, v, s, ctx, earn, chart_note))
        mark("notified")

    strict = [h for h in hits if not h[5]]
    soft_hits = [h for h in hits if h[5]]
    for lane, head_label in ((strict, "【材料×チャート一致】"), (soft_hits, "【材料×教材条件】")):
        if not lane:
            continue
        lane.sort(key=lambda h: (-h[1]["score"], -h[2].vol_ratio))
        head = f"{head_label}{now:%m/%d %H:%M}"
        if mkt[0]:
            head += f"\n地合い 日経平均 {mkt[0]}（{mkt[1]}）"
        body = [head]
        body += [fmt_hit(its, v, s, ctx, earn, mkt[0], today, note)
                 for its, v, s, ctx, earn, note in lane]
        if lane is soft_hits:
            body.append("※もともとのチャート条件（出来高1.5倍・現在値>25MA など）は未達だが、教材条件\n"
                        "　（上昇トレンド・追随期・25MA上向き・地合い良好）が揃った銘柄。未達の条件は「条件」行に併記")
        if any(h[2].after_close for h in lane):
            body.append("※引け後の株価（終値）で判定。急ぐ場合はPTS（夜間取引）の板を確認して対応可。\n"
                        "　翌営業日に持ち越す場合は寄付きのギャップを再確認してから判断")
        send("\n\n".join(body), dry_run=dry_run)
    return {"targets": len(targets), "hits": len(hits), "soft": len(soft_hits), "mkt": mkt}


def scan_market(now: datetime, state: dict, dry_run: bool, mkt: tuple | None = None,
                frames: dict | None = None) -> dict:
    """材料ニュースが無くても、教材条件を満点で満たす銘柄を拾う（引け後の日次スキャン）。

    通知条件は「上昇トレンド・追随期・25MA上向き・地合い良好」の4点（chart_context.scan_ok）。
    出来高と「現在値>25MA」は課さない。材料の出ていない銘柄の出来高が平常なのは
    当たり前だし、教材の追随期＝押し目からの反発は定義上いったん 25MA を割るので、
    どちらも狙っている場面そのものを弾いてしまう。上値余地はゲートにせず、
    room_line() で円・%・リスクリワード比を出して判断材料にする。
    RSI・ギャップ・25MA>75MA の高値掴みガードはそのまま効かせる。
    frames（{code4: 日足}）を渡せば取り直さない（急落検知と日足を共有するため）。
    """
    codes = load_universe()
    if not mkt or not mkt[0]:          # run() で取れていればそれを使い回す
        mkt = market_condition(fetch_market())
    print(f"教材スキャン 対象{len(codes)}銘柄 / 地合い {mkt[0] or '判定不能'}")
    cooldown = trading_days_ago(now.date(), config.SCAN_COOLDOWN_DAYS).isoformat()

    if callable(frames):
        frames = frames()
    if frames is None:
        frames = fetch_history_batch(codes)
    skip = news_codes(state, now.date())     # 材料ニュースの枠で扱っている銘柄は重ねて出さない
    hits, nodata = [], 0
    for code4 in codes:
        df = frames.get(code4)
        if df is None:
            nodata += 1
            continue
        if code4 in skip:
            continue
        s = evaluate(df, now, require_volume=config.SCAN_REQUIRE_VOLUME,
                     require_above_ma25=config.SCAN_REQUIRE_ABOVE_MA25)
        if not s.passed:
            continue
        ctx = analyze(df)
        if not scan_ok(ctx, mkt[0]):
            continue
        prev = state.get(f"mkt:{code4}")
        if prev and prev.get("d", "") > cooldown:
            print(f"[cool] {code4}: {prev['d']} に通知済みのため見送り")
            continue
        earn = earnings_note(next_earnings_date(code4), now.date())
        room = "新高値" if ctx.new_high else (
            "余地不明" if ctx.room_pct is None else f"余地{ctx.room_pct:+.1f}%")
        print(f"[MKT]  {code4}: {ctx.stage} {room} gap{s.gap_pct:+.1f}% RSI{s.rsi:.0f}")
        hits.append((code4, fetch_name(code4), s, ctx, earn))
        state[f"mkt:{code4}"] = {"d": now.date().isoformat(), "s": "market"}

    if nodata:
        print(f"[scan] 株価データ無し {nodata} 銘柄（コード更新漏れの可能性）")
    if hits:
        # 上値余地はゲートにしないので、代わりに余地の大きい順に並べる
        hits.sort(key=lambda h: -(999.0 if h[3].new_high else (h[3].room_pct or -99.0)))
        head = f"【教材条件クリア（材料ニュースなし）】{now:%m/%d %H:%M}"
        if mkt[0]:
            head += f"\n地合い 日経平均 {mkt[0]}（{mkt[1]}）"
        body = [head]
        body += [fmt_scan_hit(c, n, s, ctx, earn, mkt[0],
                              "教材条件クリア（追随期の押し目反発・材料ニュースなし）")
                 for c, n, s, ctx, earn in hits]
        body.append("※教材条件のみで抽出。材料ニュースは出ていないので、値動きの理由は各自で確認。\n"
                    "　上値余地は合否に使っていない（数値を見て取りに行くか判断する）")
        send("\n\n".join(body), dry_run=dry_run)
    return {"scanned": len(codes), "nodata": nodata, "hits": len(hits)}


def chart_scan(now: datetime, state: dict, dry_run: bool, mkt: tuple | None = None,
               frames=None) -> dict:
    """材料ニュースが無くても、もともとのチャート4条件が揃った銘柄を拾う（場中の毎回実行に同居）。

    条件は材料×チャート通知のチャート側そのもの: 現在値>25MA>75MA ／ RSI<70 ／
    出来高 ≥ 20日平均 × CHART_SCAN_VOLUME_RATIO（場中は経過時間で按分）／ 前日終値比 ≤ +5%。
    材料が無い分「出来高の急増」がシグナルなので、出来高条件は引け後の判定でも外さない。
    材料ニュースの枠で扱っている銘柄は除く（そちらで通知済み・保留中）。
    通知は出来高倍率の高い順に CHART_MAX_HITS 件まで。超えた分は state に残さないので
    次回実行（15分後）にまだ条件を満たしていれば拾われる。
    frames は {code4: 日足} か、それを返す呼び出し可能オブジェクト（急落検知と共有）。
    """
    codes = load_universe()
    if not mkt or not mkt[0]:
        mkt = market_condition(fetch_market())
    print(f"チャート条件スキャン 対象{len(codes)}銘柄 / 地合い {mkt[0] or '判定不能'}")
    if config.MARKET_FILTER_HARD and mkt[0] == "悪化":
        print(f"[hold] 地合い悪化のためチャート条件スキャンを保留（{mkt[1]}）")
        return {"scanned": len(codes), "nodata": 0, "hits": 0, "over": 0}
    cooldown = trading_days_ago(now.date(), config.CHART_COOLDOWN_DAYS).isoformat()
    skip = news_codes(state, now.date())

    if callable(frames):
        frames = frames()
    if frames is None:
        frames = fetch_history_batch(codes, period=config.DIP_HISTORY_PERIOD)
    cands, nodata = [], 0
    for code4 in codes:
        df = frames.get(code4)
        if df is None:
            nodata += 1
            continue
        if code4 in skip:
            continue
        prev = state.get(f"chart:{code4}")
        if prev and prev.get("d", "") > cooldown:
            continue
        s = evaluate(df, now, require_volume=True, volume_ratio=config.CHART_SCAN_VOLUME_RATIO)
        if s.passed:
            cands.append((code4, df, s))

    cands.sort(key=lambda c: -c[2].vol_ratio)     # 出来高の急増が大きい順
    hits = []
    for code4, df, s in cands[: config.CHART_MAX_HITS]:
        ctx = analyze(df)
        earn = earnings_note(next_earnings_date(code4), now.date())
        print(f"[CHT]  {code4}: 出来高{s.vol_ratio:.1f}倍 gap{s.gap_pct:+.1f}% RSI{s.rsi:.0f} {ctx.stage}")
        hits.append((code4, fetch_name(code4), s, ctx, earn))
        state[f"chart:{code4}"] = {"d": now.date().isoformat(), "s": "chart"}
    over = max(0, len(cands) - len(hits))

    if nodata:
        print(f"[chart] 株価データ無し {nodata} 銘柄")
    if hits:
        head = f"【チャート条件クリア（材料ニュースなし）】{now:%m/%d %H:%M}"
        if mkt[0]:
            head += f"\n地合い 日経平均 {mkt[0]}（{mkt[1]}）"
        body = [head]
        body += [fmt_scan_hit(c, n, s, ctx, earn, mkt[0],
                              f"チャート条件クリア（出来高 {s.vol_ratio:.1f}倍・材料ニュースなし）")
                 for c, n, s, ctx, earn in hits]
        foot = (f"※現在値>25MA>75MA・RSI<{config.RSI_MAX}・出来高≥{config.CHART_SCAN_VOLUME_RATIO:g}倍・"
                f"ギャップ≤+{config.MAX_GAP_PCT:g}% の銘柄（材料×チャート通知のチャート側と同じ条件）。\n"
                "　材料ニュースは出ていないので、出来高が増えた理由は各自で確認。総合（教材条件）も見て判断")
        if over:
            foot += f"\n　ほか {over} 銘柄も条件を満たしたが上位 {config.CHART_MAX_HITS} 件のみ通知（次回実行で再判定）"
        body.append(foot)
        if any(h[2].after_close for h in hits):
            body.append("※引け後の株価（終値）で判定。翌営業日に持ち越す場合は寄付きのギャップを再確認してから判断")
        send("\n\n".join(body), dry_run=dry_run)
    return {"scanned": len(codes), "nodata": nodata, "hits": len(hits), "over": over}


def fmt_summary(now: datetime, state: dict, tdnet: dict, market: dict | None,
                dip: dict | None = None, chart: dict | None = None) -> str:
    """その日の稼働結果。0件でも送るので「動いているのか」が分かる。"""
    today = now.date().isoformat()
    mine = [v for v in state.values() if v.get("s") not in _SCAN_STATUSES]
    notified = sum(1 for v in mine if v.get("d") == today and v.get("s") == "notified")
    pend = [v for v in mine if v.get("s") == "pending"]
    pend_today = [v for v in pend if v.get("d") == today]

    wd = "月火水木金土日"[now.weekday()]
    lines = [f"【tdnet-watch 日次サマリ】{now:%m/%d}（{wd}）{now:%H:%M}"]
    mkt = tdnet.get("mkt") or ("", "")
    if mkt[0]:
        lines.append(f"地合い 日経平均 {mkt[0]}（{mkt[1]}）")
    lines.append(
        f"TDnet 判定対象 {tdnet['targets']}件 → 材料合致 {notified + len(pend_today)}件"
        f"（通知 {notified}件 / 保留 {len(pend_today)}件）")
    if market is not None:
        extra = f"・株価データ無し {market['nodata']}銘柄" if market["nodata"] else ""
        lines.append(f"教材スキャン 対象 {market['scanned']}銘柄{extra} → 通知 {market['hits']}件")
    if chart is not None:
        # 場中の各実行で通知した分は state に残るので、1日ぶんを合算して出す
        codes = sorted(k[6:] for k, v in state.items()
                       if k.startswith("chart:") and v.get("d") == today)
        line = f"チャート条件スキャン 対象 {chart['scanned']}銘柄 → 本日 {len(codes)}件"
        if codes:
            line += f"（{', '.join(codes)}）"
        lines.append(line)
    if dip is not None:
        # 場中の各実行で通知した分は state に残るので、日次サマリでは1日ぶんを合算して出す
        codes = sorted(k[4:] for k, v in state.items()
                       if k.startswith("dip:") and v.get("d") == today)
        line = f"急落検知 本日 {len(codes)}件"
        if codes:
            line += f"（{', '.join(codes)}）"
        if dip.get("idx") is None:
            line += "・この実行は日経平均の当日値なしで判定不能"
        lines.append(line)

    if pend:
        lines.append("")
        lines.append(f"保留中（材料はあるがチャート条件が未達）{len(pend)}件")
        for v in sorted(pend, key=lambda v: (v.get("d", ""), v.get("c", "")), reverse=True):
            day = "" if v.get("d") == today else f"{str(v.get('d', ''))[5:].replace('-', '/')} "
            label = "／".join(v.get("v", {}).get("labels", [])) or "-"
            lines.append(f" {day}{v.get('c', '????')} {v.get('n', '')} {label}"
                         f" … {v.get('r', '理由不明')}")
    else:
        lines.append("")
        lines.append("保留中の銘柄は無し")
    return "\n".join(lines)


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
    ap.add_argument("--scan-market", action="store_true",
                    help="材料ニュース無しでも教材条件が満点の銘柄を通知（引け後の日次実行用）")
    ap.add_argument("--chart-scan", action="store_true",
                    help="材料ニュース無しでも、もともとのチャート4条件が揃った銘柄を通知（場中の実行に同居。16:00以降は --scan-market か --force が無ければ回さない）")
    ap.add_argument("--dip-scan", action="store_true",
                    help="悪材料なし／一過性悪材料の急落を通知（場中の実行に同居。16:00以降は --scan-market か --force が無ければ回さない）")
    ap.add_argument("--summary", action="store_true",
                    help="その実行の結果を日次サマリとして通知（0件でも送る）")
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
        if not args.no_state:
            names.save_cache()
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
    names.learn(items)   # 通知の見出しを日本語にするための社名を覚える

    state = {} if args.no_state else load_state()
    tdnet = run(items, now, state, args.dry_run)
    market = chart = dip = None
    if args.scan_market:
        frames = fetch_history_batch(load_universe())   # チャート条件スキャン・急落検知と共有する
        market = scan_market(now, state, args.dry_run, tdnet["mkt"], frames)
    else:
        # 場中は短い期間の日足で足りる。必要になった時点で1回だけ取る（両スキャンで共有）
        frames = lazy_frames(config.DIP_HISTORY_PERIOD)
    in_session = now.time() < DIP_SCAN_UNTIL or args.scan_market or args.force
    if args.chart_scan:
        if in_session:
            chart = chart_scan(now, state, args.dry_run, tdnet["mkt"], frames)
        else:
            print(f"{now:%H:%M} は引け後のためチャート条件スキャンはスキップ（引けの判定は日次実行で行う）")
    if args.dip_scan:
        if in_session:
            dip = dip_scan(now, items, state, args.dry_run, frames)
        else:
            print(f"{now:%H:%M} は引け後のため急落検知はスキップ（引けの判定は日次実行で行う）")
    if args.summary:
        send(fmt_summary(now, state, tdnet, market, dip, chart), dry_run=args.dry_run)
    if not args.no_state:
        save_state(state, today)
        names.save_cache()
    print(f"通知 {tdnet['hits']} 件 / 状態 {len(state)} 件")


if __name__ == "__main__":
    main()
