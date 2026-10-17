# duo-checker

Apple Store（日本）の **iPhone Duo（SIMフリー）の店頭受け取り在庫** を 30 秒ごとに確認し、在庫が復活したらメールで知らせる Mac 用ツールです。

- 全国 11 店舗（または指定した店舗）のどこかで在庫が復活したら通知する
- 在庫ありが続く間は再通知せず、売り切れ後に再入荷したらまた通知する
- 購入・バッグへの追加は一切しない（在庫の確認だけ）

> [!WARNING]
> Apple の[サイト利用規約](https://www.apple.com/jp/legal/internet-services/terms/site.html)は、自動化された手段でサイトにアクセス・監視することを禁じています。実験目的のツールなので、使う場合は自己責任でお願いします。

## 必要なもの

- macOS（Safari方式を使う場合はSafari）
- [uv](https://docs.astral.sh/uv/)（`curl -LsSf https://astral.sh/uv/install.sh | sh`）
- 通知メールを送るアカウント（Gmail / iCloud / Yahoo!メールなら、アドレスとアプリ用パスワードだけで設定できる）

Apple Account へのログインは不要で、その認証情報も扱いません。

## 使い方

**1. Safari の設定（Safari方式を使う場合だけ、最初の 1 回）**
Safari の 設定 → 詳細 →「Web デベロッパ用の機能を表示」をオンにし、メニューの 開発 →「Apple Events からの JavaScript を許可」を選ぶ。通常HTTPだけを使う場合は不要です。

**2. 起動**

```sh
git clone git@github.com:toyoshin5/duo-checker.git
cd duo-checker
uv run duo-checker
```

1. 初回はメール設定を聞かれ、テストメールが届く。Gmail の場合は https://myaccount.google.com/apppasswords でメール送信用のパスワードを作って貼り付ける（普段のパスワードではない。`.env` に権限 600 で保存）
2. カラーと容量を番号で選ぶ（`--color starwhite --capacity 256gb` のように引数でも指定できる）
3. `auto` では通常HTTPで監視を開始し、必要なときだけSafari方式へ切り替わります（`DUO_TRANSPORT=safari` なら監視用ウィンドウが開きます）。Ctrl+C で終了します。

「ターミナルが Safari を制御しようとしています」という確認が出たら「OK」を押してください。

**3. 常時監視にする（任意）**

```sh
uv run duo-checker install     # カラー・容量を選んで登録。ログインすると自動で起動する
uv run duo-checker status      # 状態と直近のログ
uv run duo-checker uninstall   # 解除
```

初回は「"DuoChecker"が"Safari"を制御しようとしています」という確認が出るので「OK」を押してください。動いている間は Mac がスリープしないようにします。ログアウト中は監視できず、ノートの蓋を閉じるとスリープします。

## その他のコマンド

| コマンド | 内容 |
|---|---|
| `uv run duo-checker --once` | 1 回だけ確認する |
| `uv run duo-checker setup` | メール設定をやり直す |
| `uv run duo-checker test-email` | テストメールを送る |
| `uv run duo-checker simulate` | 在庫を なし→あり→あり→なし→あり と模擬的に変化させ、通知メールが 2 通だけ届くか確かめる |
| `uv run duo-checker diagnose` | API・通信方式を少数回比較する（状態・通知を変更しない） |

## 任意の設定（`.env` に追記）

| 変数 | 既定値 | 内容 |
|---|---|---|
| `DUO_STORES` | 全店舗 | 監視する店舗番号（例: `R005,R718`） |
| `DUO_INTERVAL_SECONDS` | `30` | 確認間隔（秒、30 以上） |
| `DUO_ERROR_ALERT_MINUTES` | `10` | 在庫を取得できない状態がこの分数続いたら警告メールを送る |
| `DUO_MAC_NOTIFICATION` | `1` | `0` にすると Mac の通知センターに出さない |
| `DUO_API_MODE` | `auto` | `auto` / `pickup` / `fulfillment` |
| `DUO_TRANSPORT` | `auto` | `auto` / `http` / `safari` / `curl_cffi` |
| `DUO_CURL_IMPERSONATE` | `chrome` | `chrome`（curl_cffiの最新対応プロファイル）または `chrome124` など |
| `DUO_REQUEST_TIMEOUT_SECONDS` | `25` | 1リクエストのタイムアウト |
| `DUO_541_COOLDOWN_SECONDS` | `120` | HTTP 541 後に通信を止める時間 |
| `MAIL_TO` | 送信元と同じ | 通知先（カンマ区切りで複数可） |

店舗番号: R718 丸の内 / R079 銀座 / R224 表参道 / R128 新宿 / R119 渋谷 / R710 川崎 / R005 名古屋栄 / R711 京都 / R768 梅田 / R091 心斎橋 / R048 福岡

## ログ

- `logs/monitor.log` — 毎回の確認結果（店舗ごとの ○×）とエラー（日ごとに切り替え、30 日分保存）
- `logs/notifications.jsonl` — 通知メールの送信履歴

## 仕組み

在庫取得は、次の2つの非公開 API と3つの通信方式を切り替えられます。

| API | リクエストの要点 | JSON の店舗一覧 | 役割 |
|---|---|---|---|
| `fulfillment-messages` | `fae=true`, `pl=true`, `cppart=UNLOCKED_JP`, `parts.N`, `store`, `searchNearby=true` | `body.content.pickupMessage.stores` | 既存方式。Safariを優先 |
| `retail/pickup-message` | `pl=true`, `purchaseOption=fullPrice`, `mts.0=regular`, `parts.N`, `store`, `searchNearby=true` | `body.stores` | 新方式。通常HTTPを優先 |

既定の `auto` は、実測で有効な応答を得た `pickup-message + 通常HTTP` を最初に試し、通信エラー・不正JSON・対象店舗欠落時だけ別候補へ切り替えます。成功した候補は次回から優先します。`541` はレート制限の可能性があるため別方式へ連続切り替えせず、既定120秒冷却してから再試行します。

`DUO_TRANSPORT=safari` は購入ページを開いたSafariの同一オリジン `fetch`、`curl_cffi` はChrome TLS/HTTP2フィンガープリントを使います。固定 `chrome124` も指定できますが、curl_cffiのドキュメントが推奨する最新対応プロファイルに追従するため、既定値はバージョンなしの `chrome` です。

HTTP 200でも、要求した店舗と全SKUが応答に含まれなければ成功扱いにしません。541、タイムアウト、不正JSON、対象店舗欠落は「在庫なし」へ変換せず、状態・通知を更新しません。

`diagnose` は代表SKU `MK244J/A` とR718（丸の内）について、fulfillment+Safari、pickup+通常HTTP、pickup+Safari、fulfillment+curl_cffiを比較し、HTTPステータス・応答時間・店舗数・SKU取得状況を表示します。診断用の一時状態ファイルだけを使うため、通常の在庫状態や通知履歴は変更しません。

常時監視では、Safari を操作する許可を得るために専用の小さなアプリ `var/DuoChecker.app` を作り、その中で監視を動かします。

自動復旧の動き:

- 541 などの異常な応答のとき: Safariはページを読み込み直して1回だけ再試行し、その後は方式を連続切り替えせず冷却する
- 失敗が続くとき: 確認間隔を最大 10 分まで延ばす。10 分続いたら警告メールを送り、復旧したら復旧メールを送る
- メールの送信に失敗したとき: 次のサイクルで再送する
- 監視用ウィンドウが閉じられたとき: 自動で開き直す

## トラブルシューティング

- **Safari を操作する権限がない** → システム設定 → プライバシーとセキュリティ → オートメーション で、ターミナル（常時監視なら DuoChecker）の「Safari」をオンにする
- **ずっと 541 になる** → Safari で購入ページを開き、「ストアでの受け取り」が表示されるか確認する
- **メールが届かない** → `uv run duo-checker test-email` を実行し、`logs/notifications.jsonl` を確認する。Gmail は 2 段階認証と[アプリ パスワード](https://myaccount.google.com/apppasswords)が必要

## 開発

```sh
uv run pytest
```

`investigation/` には、実装前に購入画面と通信内容を調べたときの Playwright スクリプトがあります（`uv sync --group investigation`）。

### API調査メモ

2026-10-17に `MK244J/A`、`searchNearby=true`、店舗R718/R005/R048で確認したところ、`pickup-message` は通常HTTPでHTTP 200のJSONを返し、丸の内6店舗、名古屋栄4店舗、福岡1店舗を返しました。この3地域照会で、カタログに登録した全国11店舗を網羅できます。`fulfillment-messages` の通常HTTPとcurl_cffi（`chrome124`、`chrome`）は今回のセッションではHTTP 541＋HTMLでした。SafariのAppleScript診断はSafari側のApple Events応答がタイムアウトしたため、成功とは判定していません。これは時点・IP・Safariセッションに依存する実測であり、恒久的な成功率を意味しません。
