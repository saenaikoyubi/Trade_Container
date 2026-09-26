# Quickstart & 操作ガイド

Bybit の設定外 USDT Linear Perpetual を API から指定する受入手順を含みます。対象市場の規則は [Bybit 動的銘柄対応](bybit-dynamic-symbols.md) を参照してください。

> [!NOTE]
> 設定外 Bybit 銘柄、Mark Priceによる停止銘柄の決済、分割全決済、親処理の照会・取消、および[API仕様](api.md)で定義した既存エンドポイントの契約変更は実装前の目標仕様です。以下の該当操作例は実装後の受入確認用です。

## 1. 事前準備

ローカル開発および Paper 検証用の秘密情報ディレクトリと `.env.local` をセットアップスクリプトで自動作成します。

```powershell
# Windows PowerShell
.\scripts\setup-secrets.ps1

# Linux / macOS
./scripts/setup-secrets.sh
```

上記スクリプトにより、`$HOME/bot/secrets` 配下に秘密情報ファイル（`postgres_password`, `api_token`）およびリポジトリルートに `.env.local` が自動生成されます。手動で作成する場合は、`.env.example` を `.env.local` にコピーし、`TRADE_SECRETS_DIR` を設定してください。

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

現在の設定ファイルは[settings.json](../docker/share/volume/config/settings.json)です。Bybit、Binance、dYdXを同時に設定でき、下記はBybit USDT無期限とPaper口座に必要な目標仕様の抜粋です。現行コードには未対応の`metadata_ttl_seconds`を含みます。実装後、変更時は`trade-api`と`paper-executor`を再起動してください。

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

抜粋にない他取引所の手数料、ポーリング、市場データ鮮度、リスク上限、およびBinance・dYdXの設定は正本を参照してください。Bybit の`symbols`配列は銘柄省略時に返す既定一覧であり、明示指定できる USDT Linear Perpetual の許可一覧ではありません。Bybit のDB・APIで使うcanonical表記は`BTCUSDT`のような大文字市場IDです。

## 3. trade-ui の操作

ブラウザで `http://127.0.0.1:8080` にアクセスします。

- **ポジション管理**: ポジション一覧・未実現損益確認、個別 / 一括全決済
- **注文管理**: 未完了注文の一覧と個別取消要求
- **手動注文**: 成行・指値注文、Reduce-only 指定
- **取引制御**: Close-only の切替（新規注文受付拒否および未完了注文の取消要求）

> [!WARNING]
> **運用分離ポリシー**: Bot 稼働中は手動発注を行わず、緊急時のポジション確認・手動全決済専用として運用してください。注文の所属追跡性（`strategy_id`）の担保および意図しないポジション合算を防ぐためです。

trade-uiからの直接注文と全決済は、ブラウザから指定された値を採用せず、proxyが`strategy_id: "manual"`を強制付与します。手動操作は注文履歴の`strategy_id=manual`で抽出できます。

## 4. PowerShell CLI 操作例

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
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/positions/close" -Headers $headers -ContentType "application/json" -Body $close

# 8a. 同じ親request_idで状態・残建玉・全子注文を照会
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/close-requests/$closeId" -Headers $headers

# 8b. 必要なら親処理と残る子注文を取消
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/close-requests/$closeId/cancel" -Headers $headers

# 9. 手動UI注文の監査履歴（cursorはレスポンスのnext_cursorを次回へ渡す）
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/history/orders?strategy_id=manual&limit=100" -Headers $headers

# 10. Close-only 有効化
$closeOnly = @{ enabled = $true; reason = "manual maintenance" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/close-only" -Headers $headers -ContentType "application/json" -Body $closeOnly

# 11. Kill Switch 有効化
$killSwitch = @{ enabled = $true; reason = "emergency stop" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/kill-switch" -Headers $headers -ContentType "application/json" -Body $killSwitch
```

## 5. 障害時の確認

- `/health`が200で`/ready`が503の場合、プロセスは稼働していますが、DB・設定・有効期限内のBybit metadataのいずれかが未準備です。`checks`の`ready`と`last_error`を確認してください。Bybit metadataの目標TTLは設定可能な300秒で、期限切れmetadataを代替値として利用しません。特定銘柄の制約値だけが不正ならサービス全体のreadinessには波及しません。
- 新規注文で必要なmetadata、価格、注文板を取得できない場合は、注文を作らず`503`と`reason_code`を返します。取得できたmetadataの検査に必要な制約が不正な場合は`503 instrument_metadata_invalid`です。未知銘柄・対象外市場・取引不能状態は`422`と理由コードを確認してください。最小制約を免除する全量決済は、制約値の欠落だけでは止めません。
- 受付済み注文でmetadata・注文板・Mark Priceを取得できない場合や、実際に検査する制約が不正な場合、`paper-executor`は注文を`pending`、`open`、または`partially_filled`に戻して再試行します。`GET /api/v1/orders/by-request-id/{request_id}`の`rejection_reason`、`retry_count`、`next_attempt_at`を確認してください。不要になった注文は取消要求を送信できます。
- 全決済が`waiting`なら親処理のGETで銘柄ごとの残数量と理由を確認してください。板不足は固定回数で終了せず、取消まで再試行します。Kill Switchは未完了の子注文と親処理を取り消し、解除後も自動再開しません。
- 注文・約定履歴はDBローカル検索のため、外部取引所が停止中でも利用できます。照会時の設定にない取引所や未知symbolの指定もエラーにはならず、該当記録がなければ空ページを返します。
- サービスログは`docker compose --env-file .env.local -f docker/compose.yaml logs trade-api paper-executor`で確認できます。ログはUTC時刻を持つJSON Lines形式です。
