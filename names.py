"""銘柄コード → 日本語の社名。通知の見出しが英語表記になるのを防ぐ。

yfinance が返す社名は英語（longName = "Honda Motor Co., Ltd."）なので、そのまま
使うと通知の見出しが「■ 7267 Honda Motor Co., Ltd.」になって読みづらい。
日本語名を次の順に探し、最初に見つかったものを使う。

  1. state/names.txt   … 自分で書く対応表（1行 "7267 本田技研工業"。# 以降はコメント）
  2. nikkei225.NAMES   … 日経225の社名（リポジトリに同梱。社名変更に追随していない
                          可能性はあるが、通知が英語になるよりは読める）
  3. state/names.json  … TDnet の開示から覚えた社名（実行のたびに追記される）
  4. 見つからなければ空文字 → 呼び出し側（stock_info.fetch_name）が yfinance の
                          英語名にフォールバックするので、社名が消えることはない

TDnet の社名は「ダイキン工」のように幅で切られた略称なので、同梱の対応表より
優先順位を下げてある。日経225 の外（state/universe.txt で自分のユニバースを指定した
場合や --stock での照会）は、その銘柄が開示を出した日に覚えた名前がそのまま効く。

state/names.json は state/seen.json と同じく GitHub Actions が commit するので、
一度覚えた社名は次の実行にも残る。
"""
import json
import os
import re

from nikkei225 import NAMES as BUNDLED

OVERRIDE_PATH = "state/names.txt"
CACHE_PATH = "state/names.json"
CODE_RE = re.compile(r"\d[0-9A-Z]{3}")
MAX_CACHE = 6000   # 上場企業数より多め。壊れた入力で無限に太らせないための保険

_overrides: dict[str, str] | None = None   # None = 未読み込み
_cache: dict[str, str] | None = None
_dirty = False


def _norm(code: str) -> str:
    """TDnet式の5桁（末尾0）も4桁に揃える。"""
    c = str(code).strip().upper()
    if len(c) == 5 and c.endswith("0"):
        c = c[:4]
    return c


def load_overrides(path: str = OVERRIDE_PATH) -> dict[str, str]:
    """自分で書く対応表。1行 "コード 社名"（区切りは空白・タブ・カンマ・読点）。"""
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if not line:
                continue
            parts = re.split(r"[\s,、]+", line, maxsplit=1)
            if len(parts) != 2:
                continue
            code, name = _norm(parts[0]), parts[1].strip()
            if CODE_RE.fullmatch(code) and name:
                out[code] = name
    if out:
        print(f"[names] {path} の {len(out)} 件の社名を優先する")
    return out


def load_cache(path: str = CACHE_PATH) -> dict[str, str]:
    """TDnet から覚えた社名。壊れていれば空で始める（通知は英語名で続く）。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for k, v in data.items():
        code = _norm(k)
        if isinstance(v, str) and v.strip() and CODE_RE.fullmatch(code):
            out[code] = v.strip()
    return out


def _get_overrides() -> dict[str, str]:
    global _overrides
    if _overrides is None:
        _overrides = load_overrides(OVERRIDE_PATH)
    return _overrides


def _get_cache() -> dict[str, str]:
    global _cache
    if _cache is None:
        _cache = load_cache(CACHE_PATH)
    return _cache


def learn(items) -> None:
    """TDnet の開示（Disclosure のリスト）から社名を覚える。保存は save_cache()。"""
    global _dirty
    cache = _get_cache()
    for it in items:
        code, name = _norm(getattr(it, "code", "")), (getattr(it, "name", "") or "").strip()
        if not (CODE_RE.fullmatch(code) and name) or cache.get(code) == name:
            continue
        if code not in cache and len(cache) >= MAX_CACHE:
            continue
        cache[code] = name
        _dirty = True


def save_cache(path: str = CACHE_PATH) -> None:
    """覚えた社名を書き出す（増減が無ければ何もしない）。"""
    global _dirty
    if not _dirty or _cache is None:
        return
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(dict(sorted(_cache.items())), f, ensure_ascii=False, indent=0)
    _dirty = False


def jp_name(code4: str) -> str:
    """日本語の社名。分からなければ空文字。"""
    code = _norm(code4)
    return _get_overrides().get(code) or BUNDLED.get(code) or _get_cache().get(code) or ""


def display(code4: str, fallback: str = "") -> str:
    """通知の見出しに出す社名。日本語名が分かればそれを、無ければ fallback を使う。

    fallback は TDnet の社名（「ダイキン工」のような略称）や yfinance の英語名。
    """
    return jp_name(code4) or str(fallback or "").strip()


def reset() -> None:
    """読み込み済みの対応表を捨てる（パスを差し替えるときとテスト用）。"""
    global _overrides, _cache, _dirty
    _overrides = _cache = None
    _dirty = False
