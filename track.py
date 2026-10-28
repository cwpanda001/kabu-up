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
"""
import html
import json
import os
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


def record(track: dict, lane: str, code4: str, name: str, now: datetime, price: float,
           stop: float | None, target: float | None, verdict: str = "", url: str = "") -> bool:
    """通知1件を登録する。同じ日・同じ枠・同じ銘柄は1回だけ（False を返す）。"""
    key = f"{now.date().isoformat()}:{lane}:{code4}"
    if key in track or not price:
        return False
    track[key] = {
        "d": now.date().isoformat(), "t": f"{now:%H:%M}", "lane": lane,
        "c": code4, "n": name, "p0": round(float(price), 2),
        "stop": round(float(stop), 2) if stop else None,
        "target": round(float(target), 2) if target else None,
        "v": verdict, "url": url, "r": {},
    }
    return True


def progress(e: dict, df) -> dict:
    """1件の追跡結果 r を日足から計算する。日足に通知日以降の足が無ければ空。"""
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
    if len(after):
        r["hi"] = round(float(after["High"].max()), 2)
        r["lo"] = round(float(after["Low"].min()), 2)
        r["last"] = round(float(after["Close"].iloc[-1]), 2)
        r["last_d"] = dates[idx + len(after)].isoformat()
    r["done"] = len(after) >= HORIZONS[-1]
    return r


def update(track: dict, frames=None, fetch=None, today: date | None = None) -> dict:
    """未確定の追跡を日足で更新する。frames は {code4: 日足}（ユニバース分）、
    無い銘柄は fetch(code4) で取る（上限 TRACK_MAX_FETCH）。"""
    today = today or date.today()
    if callable(frames):
        frames = frames()
    frames = frames or {}
    todo = [e for e in track.values() if not e.get("r", {}).get("done")
            and e["d"] < today.isoformat()]      # 当日分は翌営業日以降に埋まる
    stats = {"checked": len(todo), "updated": 0, "fetched": 0, "nodata": 0}
    cache: dict = {}
    for e in todo:
        c = e["c"]
        df = frames.get(c)
        if df is None and c in cache:
            df = cache[c]
        if df is None and fetch is not None and stats["fetched"] < config.TRACK_MAX_FETCH:
            stats["fetched"] += 1
            df = cache[c] = fetch(c)
        if df is None:
            stats["nodata"] += 1
            continue
        r = progress(e, df)
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


def summarize(track: dict) -> dict:
    """枠ごとの成績。{lane: {"n":件数, "done":確定件数, "h": {1: {...}, 5: ..., 20: ...}, "target":到達率, "stop":到達率}}"""
    out: dict = {}
    for lane in list(LANES) + ["all"]:
        es = [e for e in track.values() if lane == "all" or e["lane"] == lane]
        if not es:
            continue
        s: dict = {"n": len(es), "done": sum(1 for e in es if e.get("r", {}).get("done")), "h": {}}
        for n in HORIZONS:
            rets = [_ret(e, e["r"].get(str(n))) for e in es if e.get("r", {}).get(str(n))]
            rets = [x for x in rets if x is not None]
            if rets:
                s["h"][n] = {"n": len(rets),
                             "win": sum(1 for x in rets if x > 0) / len(rets) * 100,
                             "med": statistics.median(rets),
                             "worst": min(rets), "best": max(rets)}
        with_days = [e for e in es if e.get("r", {}).get("days")]
        tg = [e for e in with_days if e.get("target")]
        st = [e for e in with_days if e.get("stop")]
        s["target"] = (sum(1 for e in tg if e["r"]["hi"] >= e["target"]) / len(tg) * 100) if tg else None
        s["stop"] = (sum(1 for e in st if e["r"]["lo"] <= e["stop"]) / len(st) * 100) if st else None
        s["target_n"], s["stop_n"] = len(tg), len(st)
        out[lane] = s
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
th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:right}th{background:var(--card);font-weight:600;position:sticky;top:0}
td:first-child,th:first-child,td.l,th.l{text-align:left}.pos{color:var(--pos)}.neg{color:var(--neg)}
.tag{display:inline-block;padding:0 6px;border-radius:4px;background:var(--card);font-size:12px}
.hit{font-weight:600}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:8px;margin:8px 0}
.card{background:var(--card);border-radius:8px;padding:10px}.card b{display:block;font-size:22px}.card span{font-size:12px;color:var(--mut)}
.btn{display:inline-block;margin:0 6px 6px 0;padding:4px 10px;border:1px solid var(--line);border-radius:14px;cursor:pointer;font-size:13px;background:var(--bg);color:var(--fg)}
.btn.on{background:var(--acc);color:#fff;border-color:var(--acc)}a{color:var(--acc)}
.note{font-size:12px;color:var(--mut);margin-top:16px}.note li{margin:2px 0}
"""

_JS = """
document.querySelectorAll('.btn').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('.btn').forEach(x=>x.classList.remove('on'));b.classList.add('on');
 const l=b.dataset.lane;document.querySelectorAll('tr[data-lane]').forEach(r=>r.style.display=(l==='all'||r.dataset.lane===l)?'':'none');});
"""


def _pct(x, digits: int = 1, plus: bool = True) -> str:
    if x is None:
        return '<td class="mut">−</td>'
    cls = "pos" if x > 0 else "neg" if x < 0 else ""
    return f'<td class="{cls}">{x:+.{digits}f}%</td>' if plus else f"<td>{x:.{digits}f}%</td>"


def _cell_price(e: dict, key: str) -> str:
    p = e.get("r", {}).get(key)
    if p is None:
        return '<td class="mut">−</td>'
    r = _ret(e, p)
    cls = "pos" if r > 0 else "neg" if r < 0 else ""
    return f'<td class="{cls}">{yen(p)}<br><small>{r:+.1f}%</small></td>'


def _row(e: dict) -> str:
    r = e.get("r", {})
    name = html.escape(e.get("n") or "")
    v = html.escape(e.get("v") or "")
    link = f' <a href="{html.escape(e["url"])}" target="_blank" rel="noopener">PDF</a>' if e.get("url") else ""
    tgt = e.get("target")
    stp = e.get("stop")
    hi_hit = bool(tgt and r.get("hi") is not None and r["hi"] >= tgt)
    lo_hit = bool(stp and r.get("lo") is not None and r["lo"] <= stp)
    hi = (f'<td class="{"pos hit" if hi_hit else ""}">{yen(r["hi"])}{" ✓" if hi_hit else ""}</td>'
          if r.get("hi") is not None else '<td class="mut">−</td>')
    lo = (f'<td class="{"neg hit" if lo_hit else ""}">{yen(r["lo"])}{" ✗" if lo_hit else ""}</td>'
          if r.get("lo") is not None else '<td class="mut">−</td>')
    status = "確定" if r.get("done") else (f'{r.get("days", 0)}日目' if r.get("days") else "待ち")
    return (f'<tr data-lane="{e["lane"]}"><td class="l">{e["d"][5:].replace("-", "/")} {e["t"]}</td>'
            f'<td class="l"><span class="tag">{LANES.get(e["lane"], e["lane"])}</span></td>'
            f'<td class="l">{e["c"]} {name}<br><small class="mut">{v}</small>{link}</td>'
            f'<td>{yen(e["p0"])}</td>{_cell_price(e, "1")}{_cell_price(e, "5")}{_cell_price(e, "20")}'
            f'<td>{yen(tgt) if tgt else "−"}</td>{hi}<td>{yen(stp) if stp else "−"}</td>{lo}<td>{status}</td></tr>')


def _summary_table(summ: dict) -> str:
    rows = []
    for lane in list(LANES) + ["all"]:
        s = summ.get(lane)
        if not s:
            continue
        label = "全体" if lane == "all" else LANES[lane]
        cells = [f'<td class="l"><b>{label}</b></td><td>{s["n"]}</td><td>{s["done"]}</td>']
        for n in HORIZONS:
            h = s["h"].get(n)
            if h:
                cells.append(f'<td>{h["win"]:.0f}%<br><small class="mut">n={h["n"]}</small></td>')
                cells.append(_pct(h["med"]))
            else:
                cells += ['<td class="mut">−</td>', '<td class="mut">−</td>']
        h20 = s["h"].get(20)
        cells.append(_pct(h20["worst"]) if h20 else '<td class="mut">−</td>')
        cells.append(f'<td>{s["target"]:.0f}%</td>' if s["target"] is not None else '<td class="mut">−</td>')
        cells.append(f'<td>{s["stop"]:.0f}%</td>' if s["stop"] is not None else '<td class="mut">−</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    head = ("<tr><th class='l'>枠</th><th>通知</th><th>確定</th>"
            "<th>1日後<br>勝率</th><th>中央値</th><th>5日後<br>勝率</th><th>中央値</th>"
            "<th>20日後<br>勝率</th><th>中央値</th><th>20日後<br>最悪</th><th>利確目安<br>到達</th><th>損切り<br>到達</th></tr>")
    return f'<div class="wrap"><table>{head}{"".join(rows)}</table></div>'


def render(track: dict, now: datetime) -> str:
    es = sorted(track.values(), key=lambda e: (e["d"], e["t"]), reverse=True)
    summ = summarize(track)
    all_ = summ.get("all") or {"n": 0, "done": 0, "h": {}}
    h20 = all_["h"].get(20)
    cards = [
        f'<div class="card"><b>{all_["n"]}</b><span>通知した銘柄（{config.TRACK_KEEP_DAYS}日以内）</span></div>',
        f'<div class="card"><b>{all_["done"]}</b><span>20日後まで確定</span></div>',
        (f'<div class="card"><b>{h20["win"]:.0f}%</b><span>20日後に上がっていた割合</span></div>'
         f'<div class="card"><b>{h20["med"]:+.1f}%</b><span>20日後の騰落率（中央値）</span></div>') if h20 else
        '<div class="card"><b>−</b><span>20日後の成績はまだ無し</span></div>',
    ]
    btns = ['<span class="btn on" data-lane="all">すべて</span>'] + [
        f'<span class="btn" data-lane="{k}">{v}</span>' for k, v in LANES.items()
        if any(e["lane"] == k for e in es)]
    head = ("<tr><th class='l'>通知</th><th class='l'>枠</th><th class='l'>銘柄</th><th>通知時</th>"
            "<th>1日後</th><th>5日後</th><th>20日後</th><th>利確目安</th><th>期間高値</th>"
            "<th>損切り目安</th><th>期間安値</th><th>状態</th></tr>")
    rows = "".join(_row(e) for e in es) or '<tr><td colspan="12" class="l mut">まだ通知がありません</td></tr>'
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tdnet-watch 成績</title><style>{_CSS}</style></head><body>
<h1>tdnet-watch 通知銘柄のその後</h1>
<div class="mut">更新 {now:%Y/%m/%d %H:%M}（株価は20分遅延の日足・引け後に1日1回更新）</div>
<div class="cards">{"".join(cards)}</div>
<h2>枠ごとの成績</h2>
{_summary_table(summ)}
<h2>通知した銘柄の一覧</h2>
<div>{"".join(btns)}</div>
<div class="wrap"><table>{head}{rows}</table></div>
<ul class="note">
<li>騰落率は<b>通知時の株価</b>からの変化。赤がプラス・青がマイナス（日本の株価ボードと同じ色）。「確定」は20営業日ぶんの値が揃った銘柄</li>
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
