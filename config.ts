// duo-checker の設定。書き換えたら `npm run deploy` で反映される。
import { defineConfig } from "./src/config";

export default defineConfig({
  // カラー: "starwhite"（スターホワイト） / "nightsky"（ナイトスカイ）。複数指定できる
  colors: ["starwhite"],

  // 容量: "256gb" / "512gb" / "1tb" / "2tb"。複数指定できる
  capacities: ["256gb"],

  // 通知先のメールアドレス。Resend で独自ドメインを認証していない場合は、
  // Resend に登録したメールアドレスにしか送れない
  mailTo: ["toyodasinngo@gmail.com"],

  // 監視する店舗。"all" なら全国 11 店舗。絞る場合は店舗番号を並べる（例: ["R005", "R718"]）
  //   R718 丸の内 / R079 銀座 / R224 表参道 / R128 新宿 / R119 渋谷 / R710 川崎
  //   R005 名古屋栄 / R711 京都 / R768 梅田 / R091 心斎橋 / R048 福岡
  stores: ["R718", "R079", "R224", "R128", "R119", "R710"],

  // 以下は省略可能（既定値のままでよい）
  intervalSeconds: 60,        // 通常は1分間隔。HTTP 541 が続くと3分から最大10分まで延長
  // errorAlertMinutes: 10,    // 在庫を取得できない状態がこの分数続いたら警告メール
  // mailFrom: "duo-checker <onboarding@resend.dev>",  // 独自ドメインを Resend で認証したら変更できる
  // enabled: true,            // false にすると監視を止める
});
