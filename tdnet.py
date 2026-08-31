"""TDnet（適時開示情報閲覧サービス）の日別一覧ページを取得してパースする。

一覧URL: https://www.release.tdnet.info/inbs/I_list_{page:03d}_{YYYYMMDD}.html
公式APIは無いのでHTMLを読む。マークアップが変わったら parse_list() を直す。
"""
import os
import re
import time
from dataclasses import dataclass
from datetime import date

import requests
from bs4 import BeautifulSoup

BASE = "https://www.release.tdnet.info/inbs/"
HEADERS = {"User-Agent": "Mozilla/5.0 (tdnet-watch; personal use)"}


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


def parse_list(html: str, d: date) -> list[Disclosure]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="main-list-table") or soup.find("table")
    if table is None:
        return []
    out = []
    for tr in table.find_all("tr"):
        if len(tr.find_all("td")) < 4:
            continue
        t, c, n, ti = (_cell(tr, "kjTime", 0), _cell(tr, "kjCode", 1),
                       _cell(tr, "kjName", 2), _cell(tr, "kjTitle", 3))
        if not (t and c and n and ti):
            continue
        a = ti.find("a")
        href = a["href"].strip() if a and a.has_attr("href") else ""
        title = (a.get_text(" ", strip=True) if a else ti.get_text(" ", strip=True))
        title = re.sub(r"\s+", " ", title)
        code = c.get_text(strip=True)
        tm = t.get_text(strip=True)
        did = os.path.basename(href) or f"{d:%Y%m%d}-{code}-{tm}-{abs(hash(title))}"
        out.append(Disclosure(
            id=did, date=d.isoformat(), time=tm, code=code,
            name=n.get_text(strip=True), title=title,
            url=(href if href.startswith("http") else BASE + href) if href else "",
        ))
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
