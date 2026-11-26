"""通知した銘柄の「その後」を追跡し、成績ページ（docs/index.html）を作る。

  python main.py --track                        # 追跡の更新とページ生成（引け後の日次実行に同居）
  python main.py --track --dry-run --no-state   # 手元で試す（ファイルは書かない）

通知のたびに record() で「いつ・どの枠で・いくらで通知したか」を state/track.json に残し、
引け後の日次実行で update() が「1日後・5日後・20日後の終値」「期間中の最高値・最安値」を
埋めていく。render() がそれを1枚の HTML にまとめ、GitHub Pages で URL として読めるようにする
（通知本文には混ぜない。日次サマリに URL を1行出すだけ）。

成績の見かた（ページにも書いてある）:
  - 騰落率は「通知時の株価」からの変化。20日後まで埋まった件を「確定」として勝率・中央値を出す
  - 利確目安到達 = 通知後20営業日のうちに高値が「上値抵抗（急落枠は前日終値）」に届いた
  - 損切り到達   = 同じ期間に安値が「損切り目安（急落枠は当日安値）」を割った
    両方に届いた場合は、どちらが先かは日足では分からないので両方に数える（保守的に見る）
  - 市場比 = 銘柄の騰落率 − 同じ期間の市場全体（config.TRACK_BENCHMARK。既定は TOPIX 連動 ETF）の騰落率。
    「条件が効いたのか、相場全体が上がっただけか」を分けるために出す
  - 条件ごとの成績 = 材料の種類（増配・自己株式取得…）や出来高倍率で分けた成績（conditions()）

記録は「1通知1行」のまま持つ（同じ銘柄が日をまたいで何度通知されても、まとめない）。通知ごとに
株価・出来高倍率・目安が違い、「何回目の通知か」も分析の対象だから。その代わり各行に
  - x   = 通知した瞬間の条件（conditions_at()。場中の出来高倍率や地合いの判定は、後から日足では復元できない）
  - seq = 同じ枠・同じ銘柄の何回目の通知か（前回から TRACK_REPEAT_DAYS 日以内なら続きとして数える）
を残し、集計の側で「初回のみ」の成績を並べて出す（同じ銘柄の重複が成績を左右していないかを見る）。
"""
import html
import json
import os
import re
import statistics
from datetime import date, datetime, timedelta

import config
from chart_context import yen

TRACK_PATH = "state/track.json"
PAGE_PATH = "docs/index.html"
HORIZONS = (1, 5, 20)
LANES = {
    "strict": "材料×チャート",
    "soft": "材料×教材条件",
    "market": "教材条件",
    "chart": "チャート条件",
    "dip": "急落",
}


def load(path: str = TRACK_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(track: dict, path: str = TRACK_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(track, f, ensure_ascii=False, indent=0)


def _num(x, digits: int = 2):
    """記録用の数値。None・NaN・数値でないものは None（＝項目ごと残さない）。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, digits) if v == v and abs(v) != float("inf") else None


def conditions_at(screen=None, ctx=None, dip=None, verdict: dict | None = None, market: str = "",
                  earnings: date | None = None, today: date | None = None,
                  vol_min: float | None = None) -> dict:
    """通知した瞬間の条件を、後から集計できる項目として1つの dict にまとめる（record() の cond に渡す）。

    screen = screener.Screen、ctx = chart_context.Context、dip = dip.Dip、verdict = judge の結果、
    market = 地合いの判定（良好／悪化…）、earnings = 次回決算の予定日、vol_min = この通知に使った出来高倍率の下限。
    値が無い項目は入れない。cfg は判定に使ったしきい値（config を変えた前後を後から区別するため）。"""
    x: dict = {}

    def put(key, value):
        if value is not None and value != "":
            x[key] = value

    src = screen if screen is not None else dip
    if src is not None:
        price = getattr(src, "price", 0) or 0
        put("vol_ratio", _num(src.vol_ratio))            # 出来高 ÷ 20日平均（場中は経過時間で按分した値）
        put("gap_pct", _num(src.gap_pct))                # 前日終値比%
        put("rsi", _num(src.rsi, 1))
        if price:
            put("ma25_pct", _num((price / src.ma25 - 1) * 100) if src.ma25 else None)   # 25日線からの乖離%
            put("ma75_pct", _num((price / src.ma75 - 1) * 100) if src.ma75 else None)
            put("atr_pct", _num(src.atr / price * 100) if src.atr else None)            # 1日の値幅の目安（ATR14）÷ 株価
        put("after_close", bool(src.after_close))        # 引けの値で判定したか（False = 場中の途中経過）
    if screen is not None:
        put("entry", screen.entry_label)
    if dip is not None:
        put("low_pct", _num(dip.low_pct))                # 当日安値の前日終値比%
        put("bounce", _num(dip.bounce, 0))               # 当日安値からの戻り率%
        put("idx_gap", _num(dip.idx_gap))                # 日経平均の当日騰落率%
        put("diff_pt", _num(dip.diff_pt))                # 銘柄 − 指数（pt）
        put("atr_mult", _num(dip.atr_mult))              # 下げ幅 ÷ ATR14
    if ctx is not None:
        put("trend", ctx.trend)
        put("stage", ctx.stage)
        put("room_pct", _num(ctx.room_pct))              # 上値抵抗までの余地%
        put("new_high", bool(ctx.new_high))
        put("rolled", bool(ctx.rolled))
        put("days_since_break", ctx.days_since_break)
        put("warnings", list(ctx.warnings) or None)
    if verdict:
        put("score", verdict.get("score"))
        put("ambiguous", bool(verdict.get("ambiguous")))
        put("ai", bool(verdict.get("ai")))
    put("market", market)
    if earnings is not None and today is not None:
        put("earn_days", (earnings - today).days)        # 次回決算までの暦日数
    cfg = {"rsi_max": config.RSI_MAX, "max_gap": config.MAX_GAP_PCT, "atr_stop": config.ATR_STOP_MULT}
    if vol_min is not None:
        cfg["vol_min"] = vol_min
    x["cfg"] = cfg
    return x


def _seq(track: dict, lane: str, code4: str, day: str) -> int:
    """同じ枠・同じ銘柄の何回目の通知か。直前の通知が TRACK_REPEAT_DAYS 日以内なら、その続きとして数える。"""
    prev = [e for e in track.values() if e.get("lane") == lane and e.get("c") == code4 and e.get("d", "") < day]
    if not prev:
        return 1
    last = max(prev, key=lambda e: e["d"])
    gap = (date.fromisoformat(day) - date.fromisoformat(last["d"])).days
    return last.get("seq", 1) + 1 if gap <= config.TRACK_REPEAT_DAYS else 1


def annotate(track: dict) -> int:
    """seq（何回目の通知か）が無い行に付ける。この項目を足す前の記録のため。付けた件数を返す。"""
    n = 0
    for e in sorted(track.values(), key=lambda e: (e.get("d", ""), e.get("t", ""))):
        if "seq" not in e and e.get("d") and e.get("lane") and e.get("c"):
            e["seq"] = _seq(track, e["lane"], e["c"], e["d"])
            n += 1
    return n


def record(track: dict, lane: str, code4: str, name: str, now: datetime, price: float,
           stop: float | None, target: float | None, verdict: str = "", url: str = "",
           cond: dict | None = None) -> bool:
    """通知1件を登録する。同じ日・同じ枠・同じ銘柄は1回だけ（False を返す）。
    cond は通知した瞬間の条件（conditions_at()）。x として残す。"""
    key = f"{now.date().isoformat()}:{lane}:{code4}"
    if key in track or not price:
        return False
    annotate(track)
    track[key] = {
        "d": now.date().isoformat(), "t": f"{now:%H:%M}", "lane": lane,
        "c": code4, "n": name, "p0": round(float(price), 2),
        "stop": round(float(stop), 2) if stop else None,
        "target": round(float(target), 2) if target else None,
        "v": verdict, "url": url, "r": {},
        "seq": _seq(track, lane, code4, now.date().isoformat()),
    }
    if cond:
        track[key]["x"] = cond
    return True


def _bench_at(bench, day: date, col: str = "Close") -> float | None:
    """市場全体（ベンチマーク）の、day 以前で最も新しい足の値。無ければ None。"""
    if bench is None:
        return None
    b = bench.dropna(subset=["Close"])
    b = b[[ts.date() <= day for ts in b.index]]
    if not len(b):
        return None
    v = float(b[col].iloc[-1])
    return round(v, 2) if v == v and v > 0 else None


def progress(e: dict, df, bench=None) -> dict:
    """1件の追跡結果 r を日足から計算する。日足に通知日以降の足が無ければ空。

    bench（市場全体の日足）を渡すと、同じ日付の値を b0（通知時）・b1/b5/b20・blast に残す。
    通知時の値は日足からしか取れないので、引け後の通知（15:30 以降）は当日終値、
    場中の通知は当日始値で代用する（場中の通知は 9 時台が大半なので始値が近い）。"""
    d = df.dropna(subset=["Close"])
    dates = [ts.date() for ts in d.index]
    d0 = date.fromisoformat(e["d"])
    idx = next((i for i, x in enumerate(dates) if x >= d0), None)
    if idx is None:
        return {}
    after = d.iloc[idx + 1: idx + 1 + HORIZONS[-1]]
    r: dict = {"days": int(len(after))}
    for n in HORIZONS:
        if len(after) >= n:
            r[str(n)] = round(float(after["Close"].iloc[n - 1]), 2)
    b0 = _bench_at(bench, dates[idx], "Close" if e.get("t", "") >= "15:30" else "Open")
    if b0 and len(after):
        r["b0"] = b0
        for n in HORIZONS:
            if len(after) >= n:
                r[f"b{n}"] = _bench_at(bench, dates[idx + n])
        r["blast"] = _bench_at(bench, dates[idx + len(after)])
    if len(after):
        r["hi"] = round(float(after["High"].max()), 2)
        r["lo"] = round(float(after["Low"].min()), 2)
        r["last"] = round(float(after["Close"].iloc[-1]), 2)
        r["last_d"] = dates[idx + len(after)].isoformat()
    r["done"] = len(after) >= HORIZONS[-1]
    return r


def update(track: dict, frames=None, fetch=None, today: date | None = None,
           fetch_many=None, bench=None) -> dict:
    """未確定の追跡を日足で更新する。frames は {code4: 日足}（ユニバース分）。

    frames に無い銘柄（TDnet 由来）は fetch_many(codes) → {code4: 日足} でまとめて取る
    （無ければ fetch(code4) で1銘柄ずつ）。1回の実行で取るのは TRACK_MAX_FETCH 銘柄までで、
    「まだ一度も株価が入っていない銘柄 → 最後の更新が古い銘柄」の順に取る。登録順に取ると、
    20営業日は確定しない古い銘柄が毎日上限を使い切り、新しい通知が永久に追跡されない。
    bench は市場全体の日足（市場比の計算用。progress() を参照）。"""
    today = today or date.today()
    annotate(track)
    if callable(frames):
        frames = frames()
    frames = frames or {}
    todo = [e for e in track.values() if not e.get("r", {}).get("done")
            and e["d"] < today.isoformat()]      # 当日分は翌営業日以降に埋まる
    stats = {"checked": len(todo), "updated": 0, "fetched": 0, "nodata": 0}
    stale: dict = {}                             # 取りに行く銘柄 → 優先度（小さいほど先）
    for e in todo:
        if frames.get(e["c"]) is None:
            pri = (bool(e.get("r")), e.get("r", {}).get("last_d", ""))
            stale[e["c"]] = min(stale.get(e["c"], pri), pri)
    want = sorted(stale, key=lambda c: stale[c])[:max(config.TRACK_MAX_FETCH, 0)]   # sorted は安定（同順位は登録順）
    cache: dict = {}
    if want and fetch_many is not None:
        cache = dict(fetch_many(want) or {})
    elif fetch is not None:
        cache = {c: fetch(c) for c in want}
    if fetch_many is not None or fetch is not None:
        stats["fetched"] = len(want)
    for e in todo:
        c = e["c"]
        df = frames.get(c)
        if df is None:
            df = cache.get(c)
        if df is None:
            stats["nodata"] += 1
            continue
        r = progress(e, df, bench)
        if r and bench is None:                  # 市場の日足が取れなかった回は、前回までの市場の値を消さない
            r.update({k: v for k, v in e.get("r", {}).items() if k.startswith("b") and k not in r})
        if r:
            e["r"] = r
            stats["updated"] += 1
    return stats


def prune(track: dict, today: date) -> dict:
    cutoff = (today - timedelta(days=config.TRACK_KEEP_DAYS)).isoformat()
    return {k: v for k, v in track.items() if v.get("d", "") >= cutoff}


def _ret(e: dict, price) -> float | None:
    if price is None or not e.get("p0"):
        return None
    return (price / e["p0"] - 1) * 100


def _xret(e: dict, key: str) -> float | None:
    """市場比（銘柄の騰落率 − 同じ期間の市場全体の騰落率）。市場の値が無ければ None。"""
    r = e.get("r", {})
    b0, b1 = r.get("b0"), r.get(f"b{key}")
    ret = _ret(e, r.get(key))
    if ret is None or not b0 or not b1:
        return None
    return ret - (b1 / b0 - 1) * 100


def _stats(es: list) -> dict:
    """件の集まりの成績。{"n":件数, "done":確定件数, "h": {1: {...}, 5: ..., 20: ...}, "target":到達率, "stop":到達率}
    h[n] の xmed / xwin / xn は市場比の中央値・市場に勝った割合・その件数（市場の値がある件だけ。無ければキー自体が無い）。"""
    s: dict = {"n": len(es), "done": sum(1 for e in es if e.get("r", {}).get("done")), "h": {}}
    for n in HORIZONS:
        rets = [_ret(e, e["r"].get(str(n))) for e in es if e.get("r", {}).get(str(n))]
        rets = [x for x in rets if x is not None]
        if rets:
            s["h"][n] = {"n": len(rets),
                         "win": sum(1 for x in rets if x > 0) / len(rets) * 100,
                         "med": statistics.median(rets),
                         "worst": min(rets), "best": max(rets)}
            xs = [x for x in (_xret(e, str(n)) for e in es) if x is not None]
            if xs:
                s["h"][n].update(xmed=statistics.median(xs), xn=len(xs),
                                 xwin=sum(1 for x in xs if x > 0) / len(xs) * 100)
    with_days = [e for e in es if e.get("r", {}).get("days")]
    tg = [e for e in with_days if e.get("target")]
    st = [e for e in with_days if e.get("stop")]
    s["target"] = (sum(1 for e in tg if e["r"]["hi"] >= e["target"]) / len(tg) * 100) if tg else None
    s["stop"] = (sum(1 for e in st if e["r"]["lo"] <= e["stop"]) / len(st) * 100) if st else None
    s["target_n"], s["stop_n"] = len(tg), len(st)
    return s


def summarize(track: dict) -> dict:
    """枠ごとの成績。{lane: _stats(...)}。"all" は全体。
    "<lane>:first" は、その枠で同じ銘柄の再通知（seq >= 2）を除いた成績（再通知がある枠だけ）。"""
    out: dict = {}
    for lane in list(LANES) + ["all"]:
        es = [e for e in track.values() if lane == "all" or e["lane"] == lane]
        if es:
            out[lane] = _stats(es)
            first = [e for e in es if e.get("seq", 1) == 1]
            if len(first) < len(es):             # 同じ銘柄の再通知がある枠だけ、初回のみの成績を並べる
                out[f"{lane}:first"] = _stats(first)
    return out


# 条件ごとの成績の行。材料は判定ラベル（"上方修正（本文推定）／増配" のように ／ 区切り）から取る
_TAG_ALIASES = (("自己株式", "自己株式取得"), ("株主優待", "株主優待"), ("賛同の意見", "TOB 賛同"))
SEQ_LABELS = ("初回の通知", "2回目以降の通知")
VOL_BUCKETS = ((3.0, "出来高 3倍未満"), (6.0, "出来高 3〜6倍"), (float("inf"), "出来高 6倍以上"))


def tags(e: dict) -> list:
    """1件が属する条件（[(グループ, 条件名), ...]）。材料が複数ある通知は複数の条件に入る。"""
    v, lane = e.get("v") or "", e.get("lane")
    if lane == "chart":
        x = (e.get("x") or {}).get("vol_ratio")
        if x is None:                            # x を残す前の記録は判定文から読む
            m = re.search(r"出来高\s*([\d.]+)\s*倍", v)
            if not m:
                return []
            x = float(m.group(1))
        return [("チャート条件", next(label for lim, label in VOL_BUCKETS if x < lim))]
    if lane == "dip":
        return [("急落", "一過性の悪材料で急落" if v.startswith("一過性悪材料") else "悪材料なしの急落")]
    if lane not in ("strict", "soft"):
        return []
    out = []
    for part in v.split("／"):
        name = re.sub(r"（.*?）", "", part).strip()
        name = next((alias for key, alias in _TAG_ALIASES if key in name), name)
        if name and ("材料", name) not in out:
            out.append(("材料", name))
    return out


def conditions(track: dict) -> list:
    """条件ごとの成績。[(グループ, 条件名, _stats(...)), ...]。
    グループは 材料 → チャート条件 → 急落 → 通知回数 の順、材料と急落は通知の多い順、出来高倍率は小さい順。"""
    groups: dict = {}
    for e in track.values():
        for g, name in tags(e):
            groups.setdefault(g, {}).setdefault(name, []).append(e)
        groups.setdefault("通知回数", {}).setdefault(SEQ_LABELS[e.get("seq", 1) > 1], []).append(e)
    vol_order = [label for _, label in VOL_BUCKETS]
    out = []
    if len(groups.get("通知回数", {})) < 2:       # 再通知が1件も無ければ、分ける意味が無い
        groups.pop("通知回数", None)
    for g in ("材料", "チャート条件", "急落", "通知回数"):
        names = groups.get(g, {})
        order = (sorted(names, key=vol_order.index) if g == "チャート条件"
                 else sorted(names, key=SEQ_LABELS.index) if g == "通知回数"
                 else sorted(names, key=lambda k: -len(names[k])))
        out += [(g, name, _stats(names[name])) for name in order]
    return out


def page_url() -> str:
    """成績ページの URL。config.TRACK_PAGE_URL が空なら GitHub Pages の既定 URL を組む。"""
    if config.TRACK_PAGE_URL:
        return config.TRACK_PAGE_URL
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


def summary_line(track: dict) -> str:
    """日次サマリに1行だけ出す。URL が分からなければ件数だけ。"""
    s = summarize(track).get("all")
    url = page_url()
    if not s:
        return f"成績ページ {url}（追跡中の銘柄なし）" if url else ""
    h20 = s["h"].get(20)
    stat = (f"20日後 勝率 {h20['win']:.0f}%・中央値 {h20['med']:+.1f}%（確定 {h20['n']}件）"
            if h20 else f"追跡中 {s['n']}件（20日後の確定はまだ無し）")
    return f"成績ページ {url}\n {stat}" if url else f"成績 {stat}"


# ---------- HTML ----------

_CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e3e3e3;--pos:#c62828;--neg:#1565c0;--card:#f6f6f6;--acc:#2b5f8a}
@media(prefers-color-scheme:dark){:root{--bg:#121417;--fg:#e8e8e8;--mut:#9a9a9a;--line:#2c3036;--pos:#ff7b72;--neg:#6cb6ff;--card:#1b1f24;--acc:#8ab4f8}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--fg);font:14px/1.6 -apple-system,"Hiragino Sans","Noto Sans JP",sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 8px;border-left:4px solid var(--acc);padding-left:8px}
.mut{color:var(--mut);font-size:12px}.wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}th{background:var(--card);font-weight:600;position:sticky;top:0;z-index:1}
td:first-child,th:first-child,td.l,th.l{text-align:left}.pos{color:var(--pos)}.neg{color:var(--neg)}
.tall{overflow:auto;max-height:calc(100vh - 24px);max-height:calc(100dvh - 24px);border:1px solid var(--line);border-radius:6px}
.tall th{border-bottom:0;box-shadow:inset 0 -1px 0 var(--line)}
.stat{border:1px solid var(--line);border-radius:6px}.stat th{border-bottom:0;box-shadow:inset 0 -1px 0 var(--line)}
.stat table{border-collapse:separate;border-spacing:0}.stat tr:last-child td{border-bottom:0}
.stat td:first-child,.stat th:first-child{position:sticky;left:0;background:var(--bg);box-shadow:inset -1px 0 0 var(--line)}
.stat th:first-child{background:var(--card);z-index:2;box-shadow:inset -1px -1px 0 var(--line)}
.stat.stuck th{box-shadow:inset 0 -1px 0 var(--line),0 2px 4px rgba(0,0,0,.12)}.stat.stuck th:first-child{box-shadow:inset -1px -1px 0 var(--line),0 2px 4px rgba(0,0,0,.12)}
th[data-t]{cursor:pointer;user-select:none}th[data-t]:hover{color:var(--acc)}
th.asc::after{content:" ▲";font-size:10px}th.desc::after{content:" ▼";font-size:10px}
.tag{display:inline-block;padding:0 6px;border-radius:4px;background:var(--card);font-size:12px}
.hit{font-weight:600}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:8px 0}
.card{background:var(--card);border-radius:8px;padding:10px}.card b{display:block;font-size:22px}.card span{font-size:12px;color:var(--mut)}
.btn{display:inline-block;margin:0 6px 6px 0;padding:4px 10px;border:1px solid var(--line);border-radius:14px;cursor:pointer;font:inherit;font-size:13px;background:var(--bg);color:var(--fg)}
.btn.on{background:var(--acc);color:#fff;border-color:var(--acc)}a{color:var(--acc)}
.find{display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:0 0 8px}
.find label{display:inline-flex;align-items:center;gap:4px;font-size:13px;white-space:nowrap;cursor:pointer}
.find input{flex:1;max-width:340px;min-width:0;padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit;font-size:16px}
.note{font-size:12px;color:var(--mut);margin-top:16px}.note li{margin:2px 0}
"""

# 一覧の絞り込み（枠ボタン × 銘柄コード・社名の入力）と、見出しクリックでの並べ替え。
# 入力は全角→半角（NFKC）にそろえ、空白・カンマ区切りで複数指定できる（どれかに当たれば表示）。
# コードは前方一致（「72」で 72xx が出る）、社名は部分一致。
_JS = """
(()=>{
const tb=document.querySelector('#list tbody'),rows=[...tb.querySelectorAll('tr[data-lane]')],
 ths=[...document.querySelectorAll('#list th')],q=document.getElementById('q'),
 cnt=document.getElementById('cnt'),none=document.getElementById('none');
const f1=document.getElementById('f1');
let lane='all',sk=0,sd=-1;
const norm=s=>(s||'').normalize('NFKC').toUpperCase();
function apply(){
 const ts=norm(q.value).split(/[\\s,、]+/).filter(Boolean);let n=0;
 rows.forEach(r=>{const c=norm(r.dataset.c),nm=norm(r.dataset.n);
  const ok=(lane==='all'||r.dataset.lane===lane)&&(!f1.checked||r.dataset.seq==='1')&&(!ts.length||ts.some(t=>c.startsWith(t)||nm.includes(t)));
  r.hidden=!ok;if(ok)n++;});
 cnt.textContent=n+' / '+rows.length+'件';none.hidden=n>0||!rows.length;
}
function sort(){
 const num=ths[sk].dataset.t==='n';
 rows.sort((a,b)=>{const x=a.cells[sk].dataset.v||'',y=b.cells[sk].dataset.v||'';
  if(!x||!y)return (!x)-(!y)||a.dataset.i-b.dataset.i;
  return (num?x-y:x.localeCompare(y))*sd||a.dataset.i-b.dataset.i;});
 rows.forEach(r=>tb.appendChild(r));
 ths.forEach((t,i)=>{t.classList.toggle('asc',i===sk&&sd>0);t.classList.toggle('desc',i===sk&&sd<0);});
}
ths.forEach((th,i)=>{if(th.dataset.t)th.onclick=()=>{sd=(i===sk)?-sd:+(th.dataset.d||1);sk=i;sort();};});
document.querySelectorAll('.btn').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('.btn').forEach(x=>x.classList.remove('on'));b.classList.add('on');lane=b.dataset.lane;apply();});
q.oninput=apply;f1.onchange=apply;apply();
})();
// 成績の表（.stat）の見出し行を、ページを下へスクロールしても画面の上端に残す（表の下端まで）
(()=>{
const ws=[...document.querySelectorAll('.stat')];let busy=false;
function fix(){busy=false;ws.forEach(w=>{const t=w.querySelector('table'),h=t.tHead,r=t.getBoundingClientRect();
 const y=Math.max(0,Math.min(-r.top,r.height-h.offsetHeight-(t.tBodies[0].rows[0]||{offsetHeight:0}).offsetHeight));
 const v=y?'translateY('+y+'px)':'';[...h.rows[0].cells].forEach(c=>c.style.transform=v);w.classList.toggle('stuck',y>0);});}
const go=()=>{if(!busy){busy=true;requestAnimationFrame(fix);}};
addEventListener('scroll',go,{passive:true});addEventListener('resize',go);fix();
})();
"""


def _pct(x, digits: int = 1, plus: bool = True) -> str:
    if x is None:
        return '<td class="mut">−</td>'
    cls = "pos" if x > 0 else "neg" if x < 0 else ""
    return f'<td class="{cls}">{x:+.{digits}f}%</td>' if plus else f"<td>{x:.{digits}f}%</td>"


def _sv(x) -> str:
    """並べ替え用の値（td の data-v）。値が無い件は空にして、並べ替えでは常に末尾へ回す。"""
    return "" if x is None else f"{x:.4f}"


def _cell_price(e: dict, key: str) -> str:
    p = e.get("r", {}).get(key)
    if p is None:
        return '<td class="mut" data-v="">−</td>'
    r = _ret(e, p)
    cls = "pos" if r > 0 else "neg" if r < 0 else ""
    x = _xret(e, key)
    xs = f' <small class="mut">市場比{x:+.1f}</small>' if x is not None else ""
    return f'<td class="{cls}" data-v="{_sv(r)}">{yen(p)}<br><small>{r:+.1f}%</small>{xs}</td>'


def _row(e: dict, i: int = 0) -> str:
    """一覧の1行。tr の data-c / data-n は銘柄コード・社名での絞り込み用、data-i は元の並び（新しい順）。
    価格の列の data-v は通知時からの騰落率（銘柄ごとに株価の桁が違うので、価格そのものでは並べない）。"""
    r = e.get("r", {})
    name = html.escape(e.get("n") or "")
    v = html.escape(e.get("v") or "")
    link = f' <a href="{html.escape(e["url"])}" target="_blank" rel="noopener">PDF</a>' if e.get("url") else ""
    tgt = e.get("target")
    stp = e.get("stop")
    hi_hit = bool(tgt and r.get("hi") is not None and r["hi"] >= tgt)
    lo_hit = bool(stp and r.get("lo") is not None and r["lo"] <= stp)
    hi = (f'<td class="{"pos hit" if hi_hit else ""}" data-v="{_sv(_ret(e, r["hi"]))}">'
          f'{yen(r["hi"])}{" ✓" if hi_hit else ""}</td>'
          if r.get("hi") is not None else '<td class="mut" data-v="">−</td>')
    lo = (f'<td class="{"neg hit" if lo_hit else ""}" data-v="{_sv(_ret(e, r["lo"]))}">'
          f'{yen(r["lo"])}{" ✗" if lo_hit else ""}</td>'
          if r.get("lo") is not None else '<td class="mut" data-v="">−</td>')
    status = "確定" if r.get("done") else (f'{r.get("days", 0)}日目' if r.get("days") else "待ち")
    lane_no = list(LANES).index(e["lane"]) if e["lane"] in LANES else len(LANES)
    code = html.escape(e["c"])
    seq = e.get("seq", 1)
    again = f' <span class="tag">{seq}回目</span>' if seq > 1 else ""
    return (f'<tr data-lane="{e["lane"]}" data-c="{code}" data-n="{name}" data-i="{i}" data-seq="{seq}">'
            f'<td class="l" data-v="{e["d"]} {e["t"]}">{e["d"][5:].replace("-", "/")} {e["t"]}</td>'
            f'<td class="l" data-v="{lane_no}"><span class="tag">{LANES.get(e["lane"], e["lane"])}</span></td>'
            f'<td class="l" data-v="{code}">{code} {name}{again}<br><small class="mut">{v}</small>{link}</td>'
            f'<td data-v="{_sv(e["p0"])}">{yen(e["p0"])}</td>'
            f'{_cell_price(e, "1")}{_cell_price(e, "5")}{_cell_price(e, "20")}'
            f'<td data-v="{_sv(_ret(e, tgt) if tgt else None)}">{yen(tgt) if tgt else "−"}</td>{hi}'
            f'<td data-v="{_sv(_ret(e, stp) if stp else None)}">{yen(stp) if stp else "−"}</td>{lo}'
            f'<td data-v="{r.get("days", 0)}">{status}</td></tr>')


def _stats_table(rows: list, first: str) -> str:
    """成績の表。rows は [(見出しセルの中身, _stats(...)), ...]、first は左端の列名。
    市場比の列は、市場の値が1件でも入っていれば出す。"""
    bench = any("xmed" in h for _, s in rows for h in s["h"].values())
    body = []
    for label, s in rows:
        cells = [f'<td class="l">{label}</td><td>{s["n"]}</td><td>{s["done"]}</td>']
        for n in HORIZONS:
            h = s["h"].get(n)
            if h:
                cells.append(f'<td>{h["win"]:.0f}%<br><small class="mut">n={h["n"]}</small></td>')
                cells.append(_pct(h["med"]))
            else:
                cells += ['<td class="mut">−</td>', '<td class="mut">−</td>']
            if bench:
                cells.append(_pct(h.get("xmed")) if h else '<td class="mut">−</td>')
        h20 = s["h"].get(20)
        cells.append(_pct(h20["worst"]) if h20 else '<td class="mut">−</td>')
        cells.append(f'<td>{s["target"]:.0f}%</td>' if s["target"] is not None else '<td class="mut">−</td>')
        cells.append(f'<td>{s["stop"]:.0f}%</td>' if s["stop"] is not None else '<td class="mut">−</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    xh = "<th>市場比<br>中央値</th>" if bench else ""
    head = (f"<tr><th class='l'>{first}</th><th>通知</th><th>確定</th>"
            + "".join(f"<th>{n}日後<br>勝率</th><th>中央値</th>{xh}" for n in HORIZONS)
            + "<th>20日後<br>最悪</th><th>利確目安<br>到達</th><th>損切り<br>到達</th></tr>")
    # Excel の「先頭行・先頭列の固定」と同じ見た目にする。左端の列（条件名）は横スクロールの枠の中で
    # sticky で固定、見出し行はページのスクロールに合わせて _JS が下へずらす（横スクロールの枠があると
    # th の position:sticky はページのスクロールには効かないため）
    return f'<div class="wrap stat"><table><thead>{head}</thead><tbody>{"".join(body)}</tbody></table></div>'


def _summary_table(summ: dict) -> str:
    rows = []
    for lane in list(LANES) + ["all"]:
        if summ.get(lane):
            rows.append((f'<b>{"全体" if lane == "all" else LANES[lane]}</b>', summ[lane]))
        if summ.get(f"{lane}:first"):
            rows.append(('<span class="mut">└ 初回の通知のみ</span>', summ[f"{lane}:first"]))
    return _stats_table(rows, "枠")


def _conditions_table(conds: list) -> str:
    return _stats_table([(f'<span class="tag">{html.escape(g)}</span> <b>{html.escape(name)}</b>', s)
                         for g, name, s in conds], "条件")


def render(track: dict, now: datetime) -> str:
    es = sorted(track.values(), key=lambda e: (e["d"], e["t"]), reverse=True)
    summ = summarize(track)
    conds = conditions(track)
    cond_html = (f"<h2>条件ごとの成績</h2>\n{_conditions_table(conds)}\n" if conds else "")
    bname = html.escape(config.TRACK_BENCHMARK_NAME)
    all_ = summ.get("all") or {"n": 0, "done": 0, "h": {}}
    h20 = all_["h"].get(20)
    cards = [
        f'<div class="card"><b>{all_["n"]}</b><span>通知した銘柄（{config.TRACK_KEEP_DAYS}日以内）</span></div>',
        f'<div class="card"><b>{all_["done"]}</b><span>20日後まで確定</span></div>',
        (f'<div class="card"><b>{h20["win"]:.0f}%</b><span>20日後に上がっていた割合</span></div>'
         f'<div class="card"><b>{h20["med"]:+.1f}%</b><span>20日後の騰落率（中央値）</span></div>') if h20 else
        '<div class="card"><b>−</b><span>20日後の成績はまだ無し</span></div>',
    ]
    btns = ['<button type="button" class="btn on" data-lane="all">すべて</button>'] + [
        f'<button type="button" class="btn" data-lane="{k}">{v}</button>' for k, v in LANES.items()
        if any(e["lane"] == k for e in es)]
    # data-t = 並べ替えの種類（s=文字 n=数値）、data-d = 最初に押したときの向き（-1 = 大きい順・新しい順）
    head = ("<tr><th class='l desc' data-t='s' data-d='-1'>通知</th><th class='l' data-t='n'>枠</th>"
            "<th class='l' data-t='s'>銘柄</th><th data-t='n'>通知時</th>"
            "<th data-t='n' data-d='-1'>1日後</th><th data-t='n' data-d='-1'>5日後</th>"
            "<th data-t='n' data-d='-1'>20日後</th><th data-t='n' data-d='-1'>利確目安</th>"
            "<th data-t='n' data-d='-1'>期間高値</th><th data-t='n' data-d='-1'>損切り目安</th>"
            "<th data-t='n' data-d='-1'>期間安値</th><th data-t='n' data-d='-1'>状態</th></tr>")
    rows = ("".join(_row(e, i) for i, e in enumerate(es))
            or '<tr><td colspan="12" class="l mut">まだ通知がありません</td></tr>')
    rows += '<tr id="none" hidden><td colspan="12" class="l mut">該当する銘柄がありません</td></tr>'
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tdnet-watch 成績</title><style>{_CSS}</style></head><body>
<h1>tdnet-watch 通知銘柄のその後</h1>
<div class="mut">更新 {now:%Y/%m/%d %H:%M}（株価は20分遅延の日足・引け後に1日1回更新）</div>
<div class="cards">{"".join(cards)}</div>
<h2>枠ごとの成績</h2>
{_summary_table(summ)}
{cond_html}<h2>通知した銘柄の一覧</h2>
<div>{"".join(btns)}</div>
<div class="find"><input id="q" type="search" placeholder="銘柄コード・社名で絞り込み（例: 7203 6758）"
 autocomplete="off" aria-label="銘柄コード・社名で絞り込み"><label><input id="f1" type="checkbox">初回の通知だけ</label><span id="cnt" class="mut"></span></div>
<div class="wrap tall"><table id="list"><thead>{head}</thead><tbody>{rows}</tbody></table></div>
<ul class="note">
<li>一覧は<b>銘柄コードを入力すると絞り込める</b>（前方一致。空白・カンマ区切りで複数可。社名でも可）。見出しを押すと並べ替え（もう一度押すと逆順）。価格の列は通知時からの騰落率の順に並ぶ</li>
<li>騰落率は<b>通知時の株価</b>からの変化。赤がプラス・青がマイナス（日本の株価ボードと同じ色）。「確定」は20営業日ぶんの値が揃った銘柄</li>
<li><b>市場比</b>は「銘柄の騰落率 − 同じ期間の市場全体（{bname}）の騰落率」。プラスなら相場全体より強かった。相場全体が上げた日・下げた日の影響を除いて、条件そのものが効いたかを見るための列。市場の通知時の値は、場中の通知は当日始値・引け後の通知は当日終値で代用している</li>
<li><b>条件ごとの成績</b>は、材料の種類・出来高倍率（20日平均比）・急落の種類で分けたもの。材料が複数ある通知はそれぞれの行に数える。n（件数）が少ない行は1〜2銘柄の値動きで大きく変わるので、目安は30件から</li>
<li>同じ銘柄が日をまたいで何度も通知されることがある（株価も条件も毎回違うので、一覧では<b>まとめずに1通知1行</b>で残す）。前回から{config.TRACK_REPEAT_DAYS}日以内の再通知には「2回目」「3回目」と付く。成績の表の<b>「初回の通知のみ」</b>は再通知を除いた数字で、同じ銘柄の重複が成績を左右していないかを見るためのもの</li>
<li>期間高値・安値は通知の翌日から20営業日のあいだの最高値・最安値。✓ は利確目安（上値抵抗。急落枠は前日終値）に届いた、✗ は損切り目安（急落枠は当日安値）を割ったことを示す。両方に付く日は、どちらが先かは日足では分からない</li>
<li>勝率は「終値が通知時より高かった割合」で、損切りして逃げた場合は含めていない。<b>リスクリワード 1.0 のとき損益分岐の勝率は 50%</b>。実際の手数料・スリッページは含まない</li>
<li>通知は判断材料であり売買の推奨ではない。この表は「仕組みが本当に効いているか」を記録するためのもの</li>
</ul>
<script>{_JS}</script></body></html>
"""


def write_page(track: dict, now: datetime, path: str = PAGE_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(render(track, now))
    nojekyll = os.path.join(os.path.dirname(path), ".nojekyll")
    if not os.path.exists(nojekyll):
        open(nojekyll, "w").close()
