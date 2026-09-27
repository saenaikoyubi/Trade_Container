# Quickstart & 操作ガイド

Bybit の設定外 USDT Linear Perpetual を API から指定する受入手順を含みます。対象市場の規則は [Bybit 動的銘柄対応](bybit-dynamic-symbols.md) を参照してください。

## 1. 事前準備

ローカル開発および Paper 検証用の秘密情報ディレクトリと `.env.local` をセットアップスクリプトで自動作成します。

```powershell
# Windows PowerShell
.\scripts\setup-secrets.ps1

# Linux / macOS
./scripts/setup-secrets.sh
```

上記スクリプトにより、`$HOME/bot/secrets` 配下に秘密情報ファイル（`postgres_password`, `api_token`, `ui_password`, `ui_session_secret`）およびリポジトリルートに `.env.local` が自動生成されます。既存環境も更新後に同じスクリプトを再実行してください。既存の秘密値は保持されます。手動で作成する場合は、`.env.example` を `.env.local` にコピーし、`TRADE_SECRETS_DIR` を設定してください。

## 2. 起動と停止

```powershell
# 起動（ビルド・バックグラウンド実行）
docker compose --env-file .env.local -f docker/compose.yaml up --build -d

# 状態確認
docker compose --env-file .env.local -f docker/compose.yaml ps

# 停止
docker compose --env-file .env.local -f docker/compose.yaml down
```

| サービス | URL | 役割 |
|---|---|---|
| `trade-api` | `http://127.0.0.1:8000` | REST API エンドポイント |
| `trade-ui` | `http://127.0.0.1:8080` | 手動確認、ポジション全決済、注文取消（緊急用） |

### 設定ファイル (`docker/share/volume/config/settings.json`)

現在の設定ファイルは[settings.json](../docker/share/volume/config/settings.json)です。Bybit、Binance、dYdXを同時に設定でき、下記はBybit USDT無期限とPaper口座に必要な設定の抜粋です。変更時は`trade-api`と`paper-executor`を再起動してください。

```json
{
  "exchanges": {
    "bybit": {
      "adapter": "ccxt",
      "options": {
        "defaultType": "linear"
      },
      "metadata_ttl_seconds": 300,
      "symbols": [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT"
      ],
      "fees": {
        "maker": "0.0002",
        "taker": "0.00055"
      }
    }
  },
  "account": {
    "currency": "USDT",
    "initial_balance": "10000.0",
    "default_leverage": "10.0"
  }
}
```

抜粋にない他取引所の手数料、ポーリング、市場データ鮮度、リスク上限、およびBinance・dYdXの設定は正本を参照してください。Bybit の`symbols`配列は銘柄省略時に返す既定一覧であり、明示指定できる USDT Linear Perpetual の許可一覧ではありません。`symbols: []`なら明示指定専用です。`exchanges: {}`も有効な設定です。Bybit のDB・APIで使うcanonical表記は`BTCUSDT`のような大文字市場IDです。

## 3. trade-ui の操作

ブラウザで `http://127.0.0.1:8080` にアクセスし、`<TRADE_SECRETS_DIR>/local/trade-ui/ui_password`の値でログインします。APIトークンをブラウザへ入力する必要はありません。セッションの有効期間は1時間で、ログアウトできます。Bybit metadataの準備障害でAPIの`/ready`が503でも、UIは起動してログインと取引制御操作を受け付けます。

- **ポジション管理**: ポジション一覧・未実現損益確認、個別 / 一括全決済
- **注文管理**: 未完了注文の一覧と個別取消要求
- **手動注文**: 成行・指値注文、Reduce-only 指定
- **取引制御**: Close-only と Kill Switch の切替（新規注文受付拒否および未完了注文の取消要求）

直接注文または全決済の送信結果が不明な場合、ブラウザは操作IDと送信内容を保存します。画面の未確定操作パネルで同じIDを照合し、必要なら同じID・同じ内容で再送します。別の操作を始める場合は履歴を確認してからパネルで明示的に破棄します。この保存は同一ブラウザ内に限られ、別端末との操作重複を防ぐ仕組みではありません。

> [!WARNING]
> **運用分離ポリシー**: Bot 稼働中は手動発注を行わず、緊急時のポジション確認・手動全決済専用として運用してください。注文の所属追跡性（`strategy_id`）の担保および意図しないポジション合算を防ぐためです。

trade-uiからの直接注文と全決済は、ブラウザから指定された値を採用せず、proxyが`strategy_id: "manual"`を強制付与します。手動操作は注文履歴の`strategy_id=manual`で抽出できます。

## 4. テストとバックアップ

```powershell
docker compose --env-file .env.local -f docker/compose.yaml --profile tools run --build --rm test
.\scripts\backup-and-verify.ps1
```

テストは専用のtmpfs上のPostgreSQL 17を使用し、本番DBへ接続しません。バックアップコマンドは生成した各アーカイブを隔離DBに復元し、検証に成功したものだけに`.verified`マーカーを作成します。詳細は[DB運用手順](database.md)を参照してください。

公開取引所APIの実通信スモークは通常のテストではスキップします。上記テストで作成したイメージを使い、ネットワーク接続可能な環境で次を実行します。認証情報や注文APIは使用しません。

```powershell
docker run --rm --network bridge -e RUN_LIVE_PUBLIC_MARKET_DATA=1 trade-container-test pytest -q -s tests/test_live_public_market_data.py
```

## 5. PowerShell CLI 操作例

```powershell
# プロセス生存確認（認証不要）
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health"

# DB・設定・Bybit metadataの準備確認（認証不要）
Invoke-RestMethod -Uri "http://127.0.0.1:8000/ready"

# 認証ヘッダー準備
$token = Get-Content "<TRADE_SECRETS_DIR>/local/trade-api/api_token" -Raw
$headers = @{ Authorization = "Bearer $($token.Trim())" }

# 設定外で、実行時にBybitでactiveなUSDT Linear Perpetualを選択する
$symbol = "XRPUSDT"

# 1. 銘柄メタデータ照会（取引状態・刻み幅・最小数量）
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/instruments?exchange_id=bybit&symbol=$symbol" -Headers $headers

# 2. Mark Priceと取得時刻の照会
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/prices?exchange_id=bybit&symbols=$symbol" -Headers $headers

# 3. 口座残高・余力照会
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/balance" -Headers $headers

# 4. 設定外銘柄へのPaper成行買い（strategy_id 付与）
$reqId = "order-$(Get-Random)"
$order = @{
    request_id = $reqId
    exchange_id = "bybit"
    symbol = $symbol
    side = "buy"
    order_type = "market"
    quantity = "10"  # 実行前にmin_qty・qty_step・min_notionalとリスク上限を確認して調整
    strategy_id = "alpha-v1"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/orders" -Headers $headers -ContentType "application/json" -Body $order

# 5. request_id による注文直接照合
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/orders/by-request-id/$reqId" -Headers $headers

# 6. 約定後のポジション・損益照会
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/current-positions?exchange_id=bybit" -Headers $headers
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/pnl" -Headers $headers

# 7. canonical symbolによる注文履歴の照合
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/history/orders?exchange_id=bybit&symbol=$symbol" -Headers $headers

# 8. 約定後の建玉全決済（202は注文群の登録。約定完了ではない）
$closeId = "close-$(Get-Random)"
$close = @{ request_id = $closeId; exchange_id = "bybit"; symbol = $symbol } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v2/positions/close" -Headers $headers -ContentType "application/json" -Body $close

# 8a. 同じ親request_idで状態・残建玉・全子注文を照会
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v2/close-requests/$closeId" -Headers $headers

# 8b. 必要なら親処理と残る子注文を取消
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v2/close-requests/$closeId/cancel" -Headers $headers

# 9. 手動UI注文の監査履歴（cursorはレスポンスのnext_cursorを次回へ渡す）
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/history/orders?strategy_id=manual&limit=100" -Headers $headers

# 10. Close-only 有効化
$closeOnly = @{ enabled = $true; reason = "manual maintenance" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/close-only" -Headers $headers -ContentType "application/json" -Body $closeOnly

# 11. Kill Switch 有効化
$killSwitch = @{ enabled = $true; reason = "emergency stop" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/kill-switch" -Headers $headers -ContentType "application/json" -Body $killSwitch
```

## 6. 障害時の確認

- `/health`が200で`/ready`が503の場合、プロセスは稼働していますが、DB・設定・有効期限内のBybit metadataのいずれかが未準備です。`checks`の`ready`と`last_error`を確認してください。Bybit metadataの目標TTLは設定可能な300秒で、期限切れmetadataを代替値として利用しません。特定銘柄の制約値だけが不正ならサービス全体のreadinessには波及しません。
- 新規注文で必要なmetadata、価格、注文板を取得できない場合は、注文を作らず`503`と`reason_code`を返します。取得できたmetadataの検査に必要な制約が不正な場合は`503 instrument_metadata_invalid`です。未知銘柄・対象外市場・取引不能状態は`422`と理由コードを確認してください。最小制約を免除する全量決済は、制約値の欠落だけでは止めません。
- 受付済み注文でmetadata・注文板・Mark Priceを取得できない場合や、実際に検査する制約が不正な場合、`paper-executor`は注文を`pending`、`open`、または`partially_filled`に戻して再試行します。`GET /api/v1/orders/by-request-id/{request_id}`の`rejection_reason`、`retry_count`、`next_attempt_at`を確認してください。不要になった注文は取消要求を送信できます。
- 全決済が`waiting`なら親処理のGETで銘柄ごとの残数量と理由を確認してください。板不足は固定回数で終了せず、取消まで再試行します。Kill Switchは未完了の子注文と親処理を取り消し、解除後も自動再開しません。
- v2全決済の`items`には、その時点で生成済みの子注文だけが表示されます。残建玉があれば子の終端処理後に次の子が増えます。v1は互換用として受付時に初期子を全件登録します。`request_id`の再送では元のバージョンを使用してください。
- Binance・dYdXのmetadata更新が失敗し、取得後1時間を超えた場合、新規・増加注文は停止し、24時間以内のlast-known-goodによるReduce-onlyだけを許容します。`GET /api/v1/instruments`の`metadata_stale`と`metadata_age_seconds`を確認してください。BybitはTTL切れの値を使用しません。設定値のNaN、無限大、負値、手数料率の範囲外はreadinessを503にします。
- 注文・約定履歴はDBローカル検索のため、外部取引所が停止中でも利用できます。照会時の設定にない取引所や未知symbolの指定もエラーにはならず、該当記録がなければ空ページを返します。
- サービスログは`docker compose --env-file .env.local -f docker/compose.yaml logs trade-api paper-executor`で確認できます。ログはUTC時刻を持つJSON Lines形式です。
