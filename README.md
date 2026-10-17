# duo-checker

Apple Store（日本）の **iPhone Duo（SIMフリー）の店頭受け取り在庫** を **Cloudflare Workers** 上で 1 分ごとに確認し、在庫が復活したらメールで知らせます。PC を起動しておく必要はありません。

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

照会の回数が多いほど Apple にブロック（HTTP 541）されやすくなります。店舗を 1 つの地域に絞ると、1 回の確認につき照会 1 回で済みます。

- 首都圏: `["R718", "R079", "R224", "R128", "R119", "R710"]`
- 名古屋・関西: `["R005", "R711", "R768", "R091"]`

Resend で独自ドメインを認証していない場合、`mailTo` には **Resend に登録したメールアドレス** しか指定できません。

**3. デプロイして API キーを登録する**

```sh
npx wrangler login                        # ブラウザで Cloudflare にログイン
npm run deploy                            # config.ts を検証してからデプロイ
npx wrangler secret put RESEND_API_KEY    # 1 でコピーしたキーを貼り付ける
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

## 仕組み

```
Cron（毎分）── 監視が止まっていないか確認
        │
Durable Object「Monitor」── 1 分ごと（intervalSeconds）のアラームで確認
   ├ GET https://www.apple.com/jp/shop/retail/pickup-message?parts.0=MK244J/A&store=R718&searchNearby=true
   │   （近隣店舗もまとめて返るので、丸の内・名古屋栄・福岡の 3 回で 11 店舗を確認）
   ├ 在庫なし（または初回）→ 在庫あり に変わった店舗だけを通知
   └ Resend でメール送信（失敗したら次の確認で再送）
```

- 在庫状態は Durable Object のストレージに保存します。整合性が強く、1 つのインスタンスで動くので、二重通知や二重実行が起きません
- `fulfillment-messages`（購入ページが使う API）は Workers からは HTTP 541 でブロックされますが、`pickup-message` は取得できます（2026-10 時点）

### エラーと自動復旧

| 状況 | 対応 |
|---|---|
| 541（Apple によるブロック） | そのサイクルの照会をやめ、状態は変えずに 5 分休む。続けば 10 分休む（ブロック中に照会を重ねない） |
| 通信エラーなど | そのサイクルの照会をやめる。3 回以上続いたら確認間隔を 2 分 → 4 分 → 8 分 → 最大 10 分まで延ばす |
| 失敗が 10 分続く | 警告メールを送る。復旧したら復旧メールを送る |
| メール送信の失敗 | 次の確認で再送し、失敗が続いたら間隔を延ばす（最大 30 分、1 日で破棄）。冪等キーで二重送信を防ぐ |

541 は、Workers が動く拠点（Durable Object の置かれたデータセンター）の通信元ごとにかかるようで、同じ時刻でも拠点によって通ったりブロックされたりします。Cloudflare の通信元は多くの利用者で共有されているため、自分の照会が少なくてもブロックされることがあります。ブロック中は在庫を確認できないので、確実性を優先するなら自宅の回線で動く [`mac-safari` ブランチ](../../tree/mac-safari) の Mac 版も検討してください。

`/?egress` を開くと、Durable Object が動いている拠点を確認できます。

### 無料枠の目安（1 分間隔・11 店舗）

| 項目 | 無料枠 | 使用量の目安 |
|---|---|---|
| Workers のリクエスト | 10 万回/日 | 約 1,500 回 |
| Durable Object のリクエスト | 10 万回/日 | 約 2,900 回 |
| Durable Object の稼働時間 | 13,000 GB 秒/日 | 約 270 GB 秒 |
| 1 回の CPU 時間 | 10 ミリ秒 | 1〜4 ミリ秒（実測） |
| Resend | 100 通/日 | 在庫が復活したときと警告のときだけ |


## 開発

```sh
npm test              # テスト
npm run typecheck     # 型チェック
npm run dev           # ローカルで実行（手元の workerd からの照会は Apple に 403 で拒否されることがある）
```
