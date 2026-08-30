"""通知。環境変数が設定されている先へ送る。どれも無ければ標準出力に出す。

  SLACK_WEBHOOK_URL                        : Slack Incoming Webhook（無制限・設定が一番簡単）
  SLACK_BOT_TOKEN + SLACK_CHANNEL          : Slack Bot（Webhookを禁止しているワークスペース向け）
  DISCORD_WEBHOOK_URL                      : Discord Webhook（無制限）
  LINE_CHANNEL_ACCESS_TOKEN + LINE_USER_ID : LINE Messaging API push（無料枠 月200通）

複数設定した場合は全部に送る。
"""
import os

import requests


def _chunks(text: str, n: int):
    while text:
        yield text[:n]
        text = text[n:]


def send(text: str, dry_run: bool = False) -> None:
    if dry_run:
        print(text)
        return
    sent = False

    wh = os.environ.get("SLACK_WEBHOOK_URL")
    if wh:
        for c in _chunks(text, 3800):
            # unfurl_links=False: TDnetのPDFリンクをSlackが展開して長くなるのを防ぐ
            r = requests.post(wh, json={"text": c, "unfurl_links": False}, timeout=20)
            if r.status_code != 200:
                print(f"[notify] Slack webhook error {r.status_code}: {r.text[:200]}")
        sent = True

    tok, ch = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_CHANNEL")
    if tok and ch:
        for c in _chunks(text, 3800):
            r = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {tok}"},
                json={"channel": ch, "text": c, "unfurl_links": False},
                timeout=20,
            )
            # chat.postMessage は失敗しても HTTP 200 を返すので body の ok を見る
            if r.status_code != 200 or not r.json().get("ok"):
                print(f"[notify] Slack bot error {r.status_code}: {r.text[:200]}")
        sent = True

    wh = os.environ.get("DISCORD_WEBHOOK_URL")
    if wh:
        for c in _chunks(text, 1900):
            r = requests.post(wh, json={"content": c}, timeout=20)
            if r.status_code >= 300:
                print(f"[notify] Discord error {r.status_code}: {r.text[:200]}")
        sent = True

    tok, uid = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"), os.environ.get("LINE_USER_ID")
    if tok and uid:
        for c in _chunks(text, 4900):
            r = requests.post(
                "https://api.line.me/v2/bot/message/push",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                json={"to": uid, "messages": [{"type": "text", "text": c}]},
                timeout=20,
            )
            if r.status_code != 200:
                print(f"[notify] LINE error {r.status_code}: {r.text[:200]}")
        sent = True

    if not sent:
        print("[notify] 通知先未設定のため標準出力へ:\n" + text)
