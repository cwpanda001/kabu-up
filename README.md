# tdnet-watch

TDnet（適時開示）を15分おきに監視し、**好材料 × 上昇トレンドのチャート** が揃った日本株だけを Slack / Discord / LINE に通知する最小構成ツール。GitHub Actions の無料枠で常駐する。

```
TDnet一覧HTML ─→ キーワード一次判定 ─→ (方向不明ならPDF本文で上方/下方推定) ─→ yfinanceでチャート条件 ─→ 通知
                                        └─ ANTHROPIC_API_KEY があれば Claude が最終判定（任意）
```

## 通知の中身

```
【材料×チャート一致】08/31 10:30

■ 7203 トヨタ自動車
 開示 10:00｜通期業績予想の修正（上方修正）に関するお知らせ
 判定 上方修正
 株価 2,850円（前日比 +1.5%）→ エントリー候補
 25MA 2,780 ／ 75MA 2,650 ／ RSI 62 ／ 出来高 3.7倍
 損切り目安 2,720円（−2ATR）
 https://www.release.tdnet.info/inbv/xxxx.pdf
```

「何円まで上がる」は出さない。出せるのは **見送り条件** と **損切り目安** まで（目標値は次段階の J-Quants バックテストで統計として出す）。

## セットアップ（30分）

### 1. リポジトリを作る
このフォルダをそのまま GitHub の新規リポジトリに push する。**Public 推奨**（Actions の実行時間が無制限。Private でも月2,000分あるので足りる）。

### 2. 通知先を決めて Secrets に登録
リポジトリの **Settings → Secrets and variables → Actions → New repository secret**。

必要なものだけ登録すればよい。**登録した通知先すべてに送る**（Slack と Discord の両方に、なども可）。
ひとつも登録しなければ Actions のログに出るだけで、その状態でも安全に動く。

| Secret 名 | 用途 | 必要性 |
|---|---|---|
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook。**一番簡単。まずこれを推奨** | 通知先はどれか1つあればよい |
| `SLACK_BOT_TOKEN` ＋ `SLACK_CHANNEL` | Slack Bot。Webhook を禁止しているワークスペース向け | 〃 |
| `DISCORD_WEBHOOK_URL` | Discord Webhook | 〃 |
| `LINE_CHANNEL_ACCESS_TOKEN` ＋ `LINE_USER_ID` | LINE Messaging API push（無料枠 月200通） | 〃 |
| `ANTHROPIC_API_KEY` | Claude が「業績予想の修正」の方向と材料の強弱を判定 | 任意（1判定 ≒ 0.3円） |

Secret は登録後に中身を再表示できない。控えは手元に残しておくこと。

#### Slack : Incoming Webhook（所要3分・推奨）

1. https://api.slack.com/apps → **Create New App** → **From scratch** → 名前 `tdnet-watch`、ワークスペースを選択
2. 左メニュー **Incoming Webhooks** → **Activate Incoming Webhooks** を On
3. 下部の **Add New Webhook to Workspace** → 通知先チャンネルを選んで **許可する**
4. 生成された `https://hooks.slack.com/services/T.../B.../...` をコピー
5. GitHub に Secret 名 `SLACK_WEBHOOK_URL` で登録

このURLを知っていれば誰でもそのチャンネルに投稿できる。**コードに直書きせず必ず Secret に入れる。**

#### Slack : Bot token（Webhook が使えない場合）

1〜2 の代わりに、アプリ作成後：

1. 左メニュー **OAuth & Permissions** → Scopes → **Bot Token Scopes** に `chat:write` を追加
2. ページ上部 **Install to Workspace** → 許可 → `xoxb-` で始まる **Bot User OAuth Token** をコピー → Secret `SLACK_BOT_TOKEN`
3. Slack で通知先チャンネルを開き `/invite @tdnet-watch` で Bot を招待する（**忘れると `not_in_channel` エラー**）
4. チャンネル名を右クリック → リンクをコピー → 末尾の `C0123ABCD` がチャンネルID → Secret `SLACK_CHANNEL`

#### Discord

サーバー設定 → 連携サービス → ウェブフック → 新しいウェブフック → チャンネルを選んで **ウェブフックURLをコピー** → Secret `DISCORD_WEBHOOK_URL`。

#### LINE

LINE公式アカウントを**通知専用に新規作成**し、Messaging API を有効化。LINE Developers で長期のチャネルアクセストークンを発行 → `LINE_CHANNEL_ACCESS_TOKEN`。チャネル基本設定の「あなたのユーザーID」→ `LINE_USER_ID`。自分のLINEでその通知用アカウントを友だち追加しておく。

無料枠は月200通。エルメ等で運用中のアカウントは配信数の枠を食うので使わない。

#### Anthropic（任意）

https://console.anthropic.com → API Keys → Create Key → Secret `ANTHROPIC_API_KEY`。
未設定でも動く（キーワード＋PDF本文のヒューリスティックで判定）。

### 3. 動作確認
Actions タブ → `tdnet-watch` → **Run workflow**。平日 9:00〜18:00 JST に手動実行すればその日の開示を実際に処理する。休場日はログに「休場日。終了」と出て正常。

ローカルで確認する場合:
```bash
pip install -r requirements.txt
python tests/test_basic.py                                          # ネット不要の単体テスト
python main.py --sample sample/tdnet_sample.html --dry-run --no-state  # サンプル開示で一通り動かす
python main.py --dry-run --force                                    # 実際のTDnetを読む（通知はしない）
```

### 4. 放置する
cron が平日 9:00〜17:59 JST に15分おきに動く。状態ファイル `state/seen.json` を bot が commit するので、同じ開示は二度通知されない。

## 判定ロジック

**ニュース（`config.py` で調整）**

- `POSITIVE_KEYWORDS`: 上方修正 / 増配 / 自己株式の取得 / 業務提携 / 株式分割 / TOB賛同 / 受注 など
- `AMBIGUOUS_KEYWORDS`: 「業績予想の修正」のように上下が分からないもの → PDF本文の「上方修正・上回る」と「下方修正・下回る」の出現数で推定。AI キーがあれば Claude が判定
- `SKIP_KEYWORDS`: 下方修正 / 減配 / 訂正 / 自社株買いの進捗報告 / 役員人事 / 決算短信 など → 無条件で捨てる

**チャート（yfinance 日足、20分遅延）**

| 条件 | 既定値 | 意図 |
|---|---|---|
| 現在値 > 25日MA > 75日MA | — | 上昇トレンド中の銘柄だけ |
| RSI14 < 70 | `RSI_MAX` | 過熱銘柄を追わない |
| 当日出来高 ≥ 20日平均 × 1.5 | `VOLUME_RATIO` | 材料に市場が反応しているか。場中は経過時間で按分 |
| 前日終値比 ≤ +5% | `MAX_GAP_PCT` | 大きく飛んだものは材料出尽くしで見送り |
| 前日終値比 ≤ +2% | `ENTRY_GAP_PCT` | これ以内なら「エントリー候補」、超えたら「様子見」 |
| 損切り目安 | 現在値 − 2×ATR14 | `ATR_STOP_MULT` |

条件未達の材料は `pending` として同日中（引け後開示は翌営業日中）15分ごとに再判定する。9:00 に出来高が足りなくても 9:30 に増えていれば通知される。

## 制限・注意

- **yfinance は非公式 API。** Yahoo 側の仕様変更やレート制限で止まることがある。止まったら `pip install -U yfinance`。GitHub の IP がブロックされたときは `MAX_SCREEN_PER_RUN` を下げる
- **TDnet に公式 API は無い。** 一覧ページの HTML を読んでいるので、マークアップが変わったら `tdnet.py` の `parse_list()` を直す
- GitHub Actions の cron は混雑時に5〜15分遅れる。「開示の瞬間」は取れない前提（そもそも20分遅延の株価なので同じ）
- 18:00 以降の開示は翌朝 9:00 の初回実行で拾う（前営業日 15:00 以降の開示を持ち越す仕様）
- ETF / REIT は yfinance に無いことが多く、自動的にスキップされる
- 通知は判断材料であって推奨ではない。最初の1〜2ヶ月は通知だけ受けて、的中率を記録してから使う

## 次の段階（このリポジトリには含まない）

1. **J-Quants Free プラン**で過去2年の日足を取り、「上方修正 × 上昇トレンド」の5日後・20日後リターンの中央値／下位25%を集計 → 通知に「過去N件 中央値+x% 最悪-y%」を付ける
2. 通知した銘柄の「その後」を自動で追跡して的中率を記録する
3. `AI_JUDGE_ALL = True` にして材料の強弱まで Claude に判定させる
