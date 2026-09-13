# Quickstart & 操作ガイド

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

設定の正本は[settings.json](../docker/share/volume/config/settings.json)です。現在はBybit、Binance、dYdXを同時に設定でき、下記はBybit USDT無期限とPaper口座に必要な最小抜粋です。変更後は`trade-api`と`paper-executor`を再起動してください。

```json
{
  "exchanges": {
    "bybit": {
      "adapter": "ccxt",
      "options": {
        "defaultType": "linear"
      },
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

抜粋にない他取引所の手数料、ポーリング、市場データ鮮度、リスク上限、およびBinance・dYdXの設定は正本を参照してください。symbolの配列がDB・APIで使うcanonical表記です。

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

# 1. 銘柄メタデータ照会（刻み幅・最小数量）
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/instruments?exchange_id=bybit" -Headers $headers

# 2. リアルタイム価格照会
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/prices?exchange_id=bybit&symbols=BTCUSDT" -Headers $headers

# 3. 口座残高・余力照会
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/balance" -Headers $headers

# 4. 注文発注（Bybit 成行買い、strategy_id 付与）
$reqId = "order-$(Get-Random)"
$order = @{
    request_id = $reqId
    exchange_id = "bybit"
    symbol = "BTCUSDT"
    side = "buy"
    order_type = "market"
    quantity = "0.01"
    strategy_id = "alpha-v1"
} | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/orders" -Headers $headers -ContentType "application/json" -Body $order

# 5. request_id による注文直接照合
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/orders/by-request-id/$reqId" -Headers $headers

# 6. ポジション・損益照会
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/current-positions?exchange_id=bybit" -Headers $headers
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/pnl" -Headers $headers

# 7. 手動UI注文の監査履歴（cursorはレスポンスのnext_cursorを次回へ渡す）
Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/history/orders?strategy_id=manual&limit=100" -Headers $headers

# 8. Close-only 有効化
$closeOnly = @{ enabled = $true; reason = "manual maintenance" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/close-only" -Headers $headers -ContentType "application/json" -Body $closeOnly

# 9. Kill Switch 有効化
$killSwitch = @{ enabled = $true; reason = "emergency stop" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/v1/kill-switch" -Headers $headers -ContentType "application/json" -Body $killSwitch
```

## 5. 障害時の確認

- `/health`が200で`/ready`が503の場合、プロセスは稼働していますが、DB・設定・Bybit metadataのいずれかが未準備です。`checks`の`ready`と`last_error`を確認してください。Bybit metadataは起動時に取得し、失敗時は1、2、4、8、16、32、60秒（以後60秒）で自動再試行します。
- 受付済み注文でmetadataまたは注文板を一時取得できない場合、`paper-executor`は注文を`pending`、`open`、または`partially_filled`に戻して再試行します。`GET /api/v1/orders/by-request-id/{request_id}`の`rejection_reason`、`retry_count`、`next_attempt_at`を確認してください。不要になった注文は取消要求を送信できます。
- 注文・約定履歴はDBローカル検索のため、外部取引所が停止中でも利用できます。現行設定にない取引所や未知symbolの指定もエラーにはならず、該当記録がなければ空ページを返します。
- サービスログは`docker compose --env-file .env.local -f docker/compose.yaml logs trade-api paper-executor`で確認できます。ログはUTC時刻を持つJSON Lines形式です。
