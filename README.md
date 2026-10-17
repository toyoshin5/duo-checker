# duo-checker

Apple Store（日本）の **iPhone Duo（SIMフリー）の店頭受け取り在庫** を **Cloudflare Workers** 上で通常 30 秒ごとに確認し、在庫が復活したらメールで知らせます。PC を起動しておく必要はありません。

- 全国 11 店舗（または指定した店舗）のどこかで在庫が復活したら通知する
- 在庫ありが続く間は再通知せず、売り切れ後に再入荷したらまた通知する
- 購入・バッグへの追加は一切しない（在庫の確認だけ）

> [!WARNING]
> Apple の[サイト利用規約](https://www.apple.com/jp/legal/internet-services/terms/site.html)は、自動化された手段でサイトにアクセス・監視することを禁じています。実験目的のツールなので、使う場合は自己責任でお願いします。

Mac の Safari を使う以前の版は [`mac-safari` ブランチ](../../tree/mac-safari) にあります。

## 必要なもの

- [Cloudflare](https://dash.cloudflare.com/sign-up) のアカウント（無料プランで動く）
- [Resend](https://resend.com/) のアカウント（メール送信。無料で 1 日 100 通まで）
- Node.js 20 以上

## セットアップ

```sh
git clone git@github.com:toyoshin5/duo-checker.git
cd duo-checker
npm install
```

**1. Resend の API キーを作る**
Resend にサインアップし、[API Keys](https://resend.com/api-keys) で「Create API Key」を押してキーをコピーする（`re_` で始まる文字列）。

**2. `config.ts` を書き換える**

```ts
colors: ["starwhite"],          // "starwhite"（スターホワイト） / "nightsky"（ナイトスカイ）
capacities: ["256gb"],          // "256gb" / "512gb" / "1tb" / "2tb"
mailTo: ["you@example.com"],    // 通知先
stores: "all",                  // 全国 11 店舗。絞るなら ["R005", "R718"] のように
```

最初の在庫確認前にバッグページを取得し、在庫 API に適用できる Cookie が得られたときだけ同じセッションで再利用します。ウォームアップに失敗しても在庫確認は続けますが、再取得は最低 60 秒空けます。HTTP 541 が返ると現在の Cookie を破棄してバッグページを読み直し、新しい Cookie で在庫 API を 1 回だけ再試行します。それでも 541 ならその回の在庫状態は不明です。店舗を 1 つの地域に絞ると、1 回の確認につき照会 1 回で済みます。発生条件は公開されていません。通常は 30 秒間隔で確認し、失敗が続く場合は次回まで 30 秒、1 分、2 分…と延ばして最大 10 分待ちます。

- 首都圏: `["R718", "R079", "R224", "R128", "R119", "R710"]`
- 名古屋・関西: `["R005", "R711", "R768", "R091"]`

Resend で独自ドメインを認証していない場合、`mailTo` には **Resend に登録したメールアドレス** しか指定できません。

**3. デプロイして API キーを登録する**

```sh
npx wrangler login                        # ブラウザで Cloudflare にログイン
npm run deploy                            # config.ts を検証してからデプロイ
npx wrangler secret put RESEND_API_KEY    # 1 でコピーしたキーを貼り付ける
npx wrangler secret put SLACK_WEBHOOK_URL  # Slack通知を使う場合だけ。対象チャンネルのIncoming Webhook URL
```

デプロイ時に表示される `https://duo-checker.<アカウント名>.workers.dev/` を開くと、すぐに監視が始まります（開かなくても 1 分以内に始まる）。このページで、直近の確認結果と通知履歴を確認できます。

**4. メールが届くか確かめる（任意）**

```sh
cp .dev.vars.example .dev.vars    # .dev.vars に API キーを書く（git 管理外）
npm run test-email                # テストメールを 1 通送る
npm run simulate                  # 在庫を なし→あり→あり→なし→あり と模擬的に変化させ、2 通だけ届くか確かめる
```

## 使い方

| やりたいこと | 方法 |
|---|---|
| 設定を変える | `config.ts` を書き換えて `npm run deploy` |
| 状況を見る | `https://duo-checker.<アカウント名>.workers.dev/` を開く（`apple` に直近の HTTP ステータス、`recentChecks` に直近 30 回の 200 / 541 の推移） |
| ログをリアルタイムで見る | `npm run tail`（過去のログは Cloudflare ダッシュボードの Workers & Pages → duo-checker → Logs） |
| 一時停止する | `config.ts` で `enabled: false` にして `npm run deploy` |
| 完全にやめる | `npx wrangler delete` |
| この PC から 1 回だけ確認する | `npm run check-once`（メールは送らない） |

`apple-http` ログには、確認時刻、HTTP ステータス、応答時間、本文サイズ、`Date`・`Retry-After`・`Server`・`Via`・`cf-ray` を記録します。応答本文や Cookie の値は記録せず、ウォームアップ結果では必要に応じてCookie名だけを記録します。Workers Logs は無料枠で1日20万ログイベント、3日間保存でき、現在の通常30秒間隔では十分な余裕があります。

Slack通知を使う場合は、Slack AppのIncoming Webhookを通知先チャンネルに追加し、表示されたWebhook URLを`SLACK_WEBHOOK_URL` Secretに登録します。Webhook URLは`config.ts`やGitに書きません。Slackへは在庫復活時の通知だけを送り、在庫なし・取得エラー・警告・復旧はメールのみです。在庫ありの継続中は重複投稿しません。

## 仕組み

```
Cron（毎分）── 監視が止まっていないか確認
        │
Durable Object「Monitor」── 通常 intervalSeconds ごと（現在は30秒）のアラームで確認。Cookieをバッグから更新して再試行した後も541なら30秒から最大10分へ延長
   ├ GET https://www.apple.com/jp/shop/bag（在庫APIに適用できるCookieが得られるまで、同じSessionで原則1回）
   ├ GET https://www.apple.com/jp/shop/retail/pickup-message?parts.0=MK244J/A&store=R718&searchNearby=true
   │   └ HTTP 541 の場合は Cookie を破棄し、バッグを読み直してから在庫 API を1回再試行
   │   （近隣店舗もまとめて返るので、丸の内・名古屋栄・福岡の 3 回で 11 店舗を確認）
   ├ 在庫なし（または初回）→ 在庫あり に変わった店舗だけを通知
   ├ Resend でメール送信（失敗したら次の確認で再送）
   └ Slack（設定時のみ）へ在庫復活だけを投稿
```

- 在庫状態は Durable Object のストレージに保存します。整合性が強く、1 つのインスタンスで動くので、二重通知や二重実行が起きません
- `fulfillment-messages`（購入ページが使う API）は過去の Workers 検証で HTTP 541 を返しました。`pickup-message` も今回の本番観察でHTTP 541を返しています。541の生成元や条件は未特定です。

### エラーと自動復旧

| 状況 | 対応 |
|---|---|
| HTTP 541 応答 | 現在の Cookie を破棄し、バッグページを読み直してから在庫 API を1回再試行する。それでも541ならそのサイクルの照会をやめ、状態は変えず、連続回数に応じて次の確認まで30秒、1分、2分…最大10分待つ。正常応答に戻れば通常の30秒間隔に戻る |
| 通信エラーなど | そのサイクルの照会をやめる。失敗が続いたら確認間隔を 30 秒 → 1 分 → 2 分 → 4 分 → 最大 10 分まで延ばす |
| 失敗が 10 分続く | 警告メールを送る。復旧したら復旧メールを送る |
| メール送信の失敗 | 次の確認で再送し、失敗が続いたら間隔を延ばす（最大 30 分、1 日で破棄）。冪等キーで二重送信を防ぐ |

2026-10-17 の検証では、`apac-ne`・`apac-se`・`weur` の各配置ヒントで実行拠点を分けられましたが、`weur` の実拠点は試行ごとに LHR / CDG と変わりました。Cloudflare trace の出口 IPv6 は各拠点で同一でした。同じ丸の内店・SKUへの1回の在庫照会は NRT・SIN・CDG の全拠点で HTTP 200 でした。この一回の比較ではAppleが配置を判定に使う証拠は得られておらず、配置分散だけで出口IPや制限枠が分かれるとは言えません。現在は通常30秒間隔で確認し、541 が続く場合は30秒から最大10分まで段階的に延ばします。

2分間隔にした後の最初の再確認（15:33:33 JST）も541でした。応答は `text/html`、127,974文字、52msで、`Retry-After` はなく、`Server: cloudflare` と `cf-ray` 末尾 `KIX` がありました。Cloudflareを経由した応答であることは分かりますが、541をCloudflareのエッジが生成したか、Apple側が返したかはこのヘッダーだけでは分かりません（[`cf-ray` の説明](https://developers.cloudflare.com/fundamentals/reference/http-headers/)）。その後の観測では541が続いたため、現在は通常30秒間隔で確認し、連続回数に応じて30秒から10分まで間隔を延ばします。

`/?egress` を開くと、Durable Object が動いている拠点を確認できます。

### 無料枠の目安（30 秒間隔・11 店舗）

| 項目 | 無料枠 | 使用量の目安 |
|---|---|---|
| Workers のリクエスト | 10 万回/日 | 約 3,000 回 |
| Durable Object のリクエスト | 10 万回/日 | 約 5,800 回 |
| Durable Object の稼働時間 | 13,000 GB 秒/日 | 約 540 GB 秒 |
| 1 回の CPU 時間 | 10 ミリ秒 | 1〜4 ミリ秒（実測） |
| Resend | 100 通/日 | 在庫が復活したときと警告のときだけ |


## 開発

```sh
npm test              # テスト
npm run typecheck     # 型チェック
npm run dev           # ローカルで実行（手元の workerd からの照会は Apple に 403 で拒否されることがある）
```
