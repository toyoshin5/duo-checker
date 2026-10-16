# duo-checker

Apple Store（日本）の **iPhone Duo（SIMフリー）の店頭受け取り在庫** を 1 分ごとに確認し、在庫が復活したらメールで知らせる Mac 用ツールです。

- 全国 11 店舗（または指定した店舗）のどこかで在庫が復活したら通知する
- 在庫ありが続く間は再通知せず、売り切れ後に再入荷したらまた通知する
- 購入・バッグへの追加は一切しない（在庫の確認だけ）

> [!WARNING]
> Apple の[サイト利用規約](https://www.apple.com/jp/legal/internet-services/terms/site.html)は、自動化された手段でサイトにアクセス・監視することを禁じています。実験目的のツールなので、使う場合は自己責任でお願いします。

## 必要なもの

- macOS と Safari
- [uv](https://docs.astral.sh/uv/)（`curl -LsSf https://astral.sh/uv/install.sh | sh`）
- 通知メールを送るアカウント（Gmail / iCloud / Yahoo!メールなら、アドレスとアプリ用パスワードだけで設定できる）

Apple Account へのログインは不要で、その認証情報も扱いません。

## 使い方

**1. Safari の設定（最初の 1 回だけ）**
Safari の 設定 → 詳細 →「Web デベロッパ用の機能を表示」をオンにし、メニューの 開発 →「Apple Events からの JavaScript を許可」を選ぶ。

**2. 起動**

```sh
git clone git@github.com:toyoshin5/duo-checker.git
cd duo-checker
uv run duo-checker
```

1. 初回はメール設定を聞かれ、テストメールが届く。Gmail の場合は https://myaccount.google.com/apppasswords でメール送信用のパスワードを作って貼り付ける（普段のパスワードではない。`.env` に権限 600 で保存）
2. カラーと容量を番号で選ぶ（`--color starwhite --capacity 256gb` のように引数でも指定できる）
3. Safari に監視用のウィンドウが開いて Dock に最小化され、監視が始まる（Ctrl+C で終了）

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

## 任意の設定（`.env` に追記）

| 変数 | 既定値 | 内容 |
|---|---|---|
| `DUO_STORES` | 全店舗 | 監視する店舗番号（例: `R005,R718`） |
| `DUO_INTERVAL_SECONDS` | `60` | 確認間隔（秒、30 以上） |
| `DUO_ERROR_ALERT_MINUTES` | `10` | 在庫を取得できない状態がこの分数続いたら警告メールを送る |
| `DUO_MAC_NOTIFICATION` | `1` | `0` にすると Mac の通知センターに出さない |
| `MAIL_TO` | 送信元と同じ | 通知先（カンマ区切りで複数可） |

店舗番号: R718 丸の内 / R079 銀座 / R224 表参道 / R128 新宿 / R119 渋谷 / R710 川崎 / R005 名古屋栄 / R711 京都 / R768 梅田 / R091 心斎橋 / R048 福岡

## ログ

- `logs/monitor.log` — 毎回の確認結果（店舗ごとの ○×）とエラー（日ごとに切り替え、30 日分保存）
- `logs/notifications.jsonl` — 通知メールの送信履歴

## 仕組み

Apple の在庫 API（`/jp/shop/fulfillment-messages`）は、curl や Playwright などの自動化されたクライアントには **HTTP 541** を返します。そこで、普段使っている Safari に監視用のウィンドウを 1 つ開き、AppleScript でそのページの中から在庫 API を `fetch` します。購入ページ自身が出すリクエストと同じ形です。

常時監視では、Safari を操作する許可を得るために専用の小さなアプリ `var/DuoChecker.app` を作り、その中で監視を動かします。

自動復旧の動き:

- 541 などの異常な応答のとき: ページを読み込み直して再試行する
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
