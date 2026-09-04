"""TDnet（適時開示情報閲覧サービス）の日別一覧ページを取得してパースする。

一覧URL: https://www.release.tdnet.info/inbs/I_list_{page:03d}_{YYYYMMDD}.html
公式APIは無いのでHTMLを読む。マークアップが変わったら parse_list() を直す。
"""
import hashlib
import os
import re
import time
from dataclasses import dataclass
from datetime import date

import requests
from bs4 import BeautifulSoup

BASE = "https://www.release.tdnet.info/inbs/"
HEADERS = {"User-Agent": "Mozilla/5.0 (tdnet-watch; personal use)"}

# 開示行かどうかの判定。一覧ページの末尾には「Copyright © Tokyo Stock Exchange…」の
# フッター行があり、これも td が4つ以上あるので行数だけでは弾けない。kj* クラスが
# 無いと _cell() が位置で拾ってしまい、時刻やコードが著作権表示のまま開示として
# 取り込まれていた（state/seen.json にゴミが溜まる原因）。開示行なら必ず満たす形
# ——時刻が HH:MM、コードが4〜5文字の銘柄コード——を積極的に確認して選別する。
# \d は Unicode 対応で全角数字にも一致するため、半角に限定して [0-9] と書く
TIME_RE = re.compile(r"^[0-9]{1,2}:[0-9]{2}$")
CODE_RE = re.compile(r"^[0-9][0-9A-Za-z]{3}[0-9]?$")


@dataclass
class Disclosure:
    id: str       # PDFファイル名（一意）
    date: str     # YYYY-MM-DD
    time: str     # HH:MM
    code: str     # 5桁コード（例 72030）
    name: str
    title: str
    url: str      # PDF URL

    @property
    def code4(self) -> str:
        return self.code[:4]


def _decode(content: bytes) -> str:
    head = content[:2048].decode("ascii", errors="ignore")
    m = re.search(r'charset=["\']?([\w-]+)', head, re.I)
    enc = m.group(1) if m else "utf-8"
    try:
        return content.decode(enc, errors="replace")
    except LookupError:
        return content.decode("utf-8", errors="replace")


def _cell(tr, cls, idx):
    td = tr.find("td", class_=cls)
    if td is None:
        tds = tr.find_all("td")
        td = tds[idx] if idx < len(tds) else None
    return td


def _text(td) -> str:
    """セルの表示文字列。全角空白・ノーブレークスペースも空白として畳む。"""
    return re.sub(r"\s+", " ", td.get_text(" ", strip=True)).strip()


def make_id(d: date, code: str, tm: str, title: str) -> str:
    """PDFリンクが無い行のフォールバックID。

    以前は abs(hash(title)) を使っていたが、Python の文字列ハッシュは
    PYTHONHASHSEED でプロセスごとにランダム化されるため、同じ行が実行のたびに
    別IDになり state/seen.json に積み上がっていた。プロセスをまたいでも同じ値に
    なる SHA-1 に変える。
    """
    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]
    return f"{d:%Y%m%d}-{code}-{tm}-{digest}"


def parse_list(html: str, d: date) -> list[Disclosure]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="main-list-table") or soup.find("table")
    if table is None:
        return []
    out, dropped = [], 0
    for tr in table.find_all("tr"):
        if len(tr.find_all("td")) < 4:
            continue
        t, c, n, ti = (_cell(tr, "kjTime", 0), _cell(tr, "kjCode", 1),
                       _cell(tr, "kjName", 2), _cell(tr, "kjTitle", 3))
        if not (t and c and n and ti):
            continue
        tm, code = _text(t), _text(c)
        if not (TIME_RE.match(tm) and CODE_RE.match(code)):
            dropped += 1        # フッター行・ページャ行など、開示ではない行
            continue
        a = ti.find("a")
        href = a["href"].strip() if a and a.has_attr("href") else ""
        title = _text(a) if a else _text(ti)
        if not title:
            dropped += 1
            continue
        did = os.path.basename(href) or make_id(d, code, tm, title)
        out.append(Disclosure(
            id=did, date=d.isoformat(), time=tm, code=code,
            name=_text(n), title=title,
            url=(href if href.startswith("http") else BASE + href) if href else "",
        ))
    # 開示行が1件も取れずに落とした行だけがある = マークアップ変更の可能性。
    # フッターを黙って捨てるだけのとき（通常）は何も出さない。
    if dropped and not out:
        print(f"[tdnet] {d} の一覧で開示行を1件も認識できなかった"
              f"（{dropped}行を非開示として除外）。マークアップ変更の可能性")
    return out


def fetch_day(d: date, max_pages: int = 30) -> list[Disclosure]:
    """指定日の全ページを取得。存在しないページ／空ページで打ち切る。"""
    items: list[Disclosure] = []
    for p in range(1, max_pages + 1):
        url = f"{BASE}I_list_{p:03d}_{d:%Y%m%d}.html"
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
        except requests.RequestException as e:
            print(f"[tdnet] fetch error {url}: {e}")
            break
        if r.status_code != 200:
            if p == 1:
                print(f"[tdnet] HTTP {r.status_code} {url}")
            break
        page = parse_list(_decode(r.content), d)
        if not page:
            break
        items.extend(page)
        time.sleep(0.5)
    # 同一IDの重複除去（ページ跨ぎで稀に重複する）
    seen, uniq = set(), []
    for it in items:
        if it.id not in seen:
            seen.add(it.id)
            uniq.append(it)
    return uniq
