# API 仕様

`trade-api` は HTTP REST API を提供します。`/health`, `/ready` 以外の全エンドポイントで `Authorization: Bearer <token>` が必須です。

## エンドポイント一覧

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/health` / `/ready` | ヘルスチェック（Paper環境属性付き） |
| `GET` | `/api/v1/exchanges` | 設定済み取引所・許可銘柄一覧 |
| `GET` | `/api/v1/instruments` | 銘柄メタデータ照会（刻み幅・最小数量等） |
| `GET` | `/api/v1/prices` | リアルタイム価格照会（仲値・Mark・Last） |
| `GET` | `/api/v1/balance` | 口座残高・余力照会 |
| `POST` | `/api/v1/orders` | 注文受付（監査用 `strategy_id`・数量バリデーション対応） |
| `GET` | `/api/v1/orders/{order_id}` | 注文状態取得 |
| `GET` | `/api/v1/orders/by-request-id/{request_id}` | `request_id` による注文状態直接照合 |
| `POST` | `/api/v1/orders/{order_id}/cancel` | 注文取消要求 |
| `GET` | `/api/v1/orders/{order_id}/fills` | 個別注文の約定明細 |
| `GET` | `/api/v1/fills` | 直近約定明細一覧（最大1000件） |
| `GET` | `/api/v1/positions` | 保存済みポジション一覧 |
| `GET` | `/api/v1/current-positions` | 現在価格・未実現損益付きポジション照会 |
| `POST` | `/api/v1/positions/close` | 指定ポジションの全決済（Reduce-only 成行注文生成） |
| `GET` | `/api/v1/pnl` | 実現損益合計 |
| `GET` | `/api/v1/history/orders` | 注文履歴（`strategy_id` フィルタ・ページネーション対応） |
| `GET` | `/api/v1/history/fills` | 約定履歴 |
| `GET` | `/api/v1/history/pnl` | 日次損益履歴 |
| `GET` | `/api/v1/trading-control` | Close-only / Kill Switch 状態取得 |
| `POST` | `/api/v1/close-only` | Close-only 設定更新 |
| `POST` | `/api/v1/kill-switch` | Kill Switch 設定更新 |

## 共通規則

- 現在状態を扱う注文・全決済・ポジション・市場情報APIでは、`exchange_id` と `symbol` を現行の `settings.json` に照合します。設定symbolをcanonical表記としてDBへ保存し、取引所metadataで完全一致するaliasだけをcanonicalへ変換します。比較はcase-sensitiveで、前後空白は `422 Unprocessable Entity` です。
- 履歴APIは監査記録を優先するため例外です。現行設定にない取引所・廃止symbolもDBローカルで検索でき、外部市場データには依存しません。詳細は「注文・約定履歴」を参照してください。
- 主なエラー区分は、認証失敗が`401`、対象なしが`404`、冪等性キーや取引制御との競合が`409`、入力・許可条件違反が`422`、必要なmetadata・市場価格・評価情報を取得できない一時障害が`503`です。

## エンドポイント詳細仕様

### 1. ヘルスチェック (`GET /health` / `GET /ready`)

認証不要でシステムの稼働状態および環境属性を確認できます。

`/health` はプロセスの生存、`/ready` はリクエストを安全に処理できる状態を表します。`/ready` はDB接続、設定ファイルの読み込み、設定にBybitがある場合のBybit metadataを必須条件とします。取得済みmetadataは更新失敗時も最大24時間利用し、それを超えた場合は503へ戻ります。Binance・dYdXの障害はサービス全体のreadinessへ波及させません。

**`/health` レスポンス例**:
```json
{
  "status": "ok",
  "environment": "paper",
  "simulation": true,
  "timestamp": "2026-09-10T00:15:00.123456Z"
}
```

**`/ready` 成功レスポンス例**:
```json
{
  "status": "ready",
  "environment": "paper",
  "simulation": true,
  "timestamp": "2026-09-10T00:15:00.123456+00:00",
  "checks": {
    "database": {"ready": true},
    "configuration": {"ready": true, "last_error": null},
    "bybit_metadata": {"required": true, "ready": true, "last_error": null}
  }
}
```

失敗時も同じ`checks`構造を持つ`503 Service Unavailable`を返し、`status`は`not_ready`です。

---

### 2. 注文受付 (`POST /api/v1/orders`)

新規注文を受け付け、PostgreSQL の注文キューへ登録します（`202 Accepted`）。

**リクエスト例 (Bybit 成行買い)**:
```json
{
  "request_id": "alpha-v1-20260910-001",
  "exchange_id": "bybit",
  "symbol": "BTCUSDT",
  "side": "buy",
  "order_type": "market",
  "quantity": "0.01",
  "reduce_only": false,
  "strategy_id": "alpha-momentum-v1"
}
```

**リクエスト例 (Binance 指値買い)**:
```json
{
  "request_id": "strategy-a-20260830-001",
  "exchange_id": "binance",
  "symbol": "BTC/USDT",
  "side": "buy",
  "order_type": "limit",
  "quantity": "0.01",
  "limit_price": "60000",
  "reduce_only": false,
  "strategy_id": "grid-v2"
}
```

**仕様・バリデーション**:
- `request_id`: グローバルに一意な冪等性キー。既存注文の確認はmetadata検査より先に行います。同一キーかつ同一内容（canonical symbol、`strategy_id`を含む）の再送は、既存注文を`200 OK`で返します。内容が異なる場合は `409 Conflict`。同じcanonical symbolを再送した場合は、外部metadata障害中でも既存注文を照合できます。
- `request_id` は `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` に制限。既存のslash入りlegacy IDは直接照合APIの対象外。
- `strategy_id` (`Optional[str]`): 発注元の Bot / 戦略を識別する監査タグ。
- `symbol`: Bybit ネイティブ表記（`BTCUSDT`）および CCXT 統一表記（`BTC/USDT:USDT`）の双方を受理し、設定上のcanonical表記へ正規化してDB保存・返却。
- `order_type`: `limit`（`limit_price` 必須）または `market`。
- **銘柄別数量バリデーション**: 受付時に以下の静的制約を同期検査し、違反時は `422 Unprocessable Entity` を返却（`paper-executor` でも執行直前に二重検査）。
  - 最小数量: `quantity >= min_qty`
  - 刻み幅: `(quantity - min_qty) % qty_step == 0`
  - 最小 Notional: `quantity * price >= min_notional`（※市場メタデータに `min_notional` が定義されている場合のみ適用。Limit注文: `limit_price`、Market注文: 直近仲値 `mid_price`。市場データ取得不能時は同期検査をバイパスし Executor へ委任）
- **Reduce-only 特例**: `reduce_only=true` かつ「既存ポジション全量を決済する注文」については、端数（dust）残留による解消不能を防ぐため、`qty_step` / `min_notional` / `min_qty` の制約を免除。

---

### 3. `request_id` 直接照合 (`GET /api/v1/orders/by-request-id/{request_id}`)

発注側クライアントが発注直後の状態ポーリングを行えるよう、`request_id` のユニークインデックスを利用して直接1件の注文情報を照合します。

- **Path**: `GET /api/v1/orders/by-request-id/{request_id}`
- **Response**: `OrderView`（未存在時は `404 Not Found`）

**レスポンス例**:
```json
{
  "id": "c3b5f5c2-7e14-4bb7-b9d5-8351297d171f",
  "request_id": "alpha-v1-20260910-001",
  "strategy_id": "alpha-momentum-v1",
  "exchange_id": "bybit",
  "exchange_network": "mainnet",
  "symbol": "BTCUSDT",
  "side": "buy",
  "order_type": "market",
  "quantity": "0.010",
  "limit_price": null,
  "status": "filled",
  "rejection_reason": null,
  "reduce_only": false,
  "filled_quantity": "0.010",
  "average_fill_price": "62150.50",
  "total_fee": "0.341828",
  "cancellation_requested": false,
  "resting_since": null,
  "last_market_data_id": "1538f6cd2a17d6cab201e967cd635ff66f8d4d5adadefcd4babe268d9c24d565",
  "retry_count": 0,
  "next_attempt_at": "2026-09-10T00:15:00.123456Z",
  "created_at": "2026-09-10T00:15:00.123456Z",
  "updated_at": "2026-09-10T00:15:01.045123Z"
}
```

---

### 4. 銘柄メタデータ照会 (`GET /api/v1/instruments`)

発注前の端数処理やポートフォリオ丸めに必要な銘柄仕様（刻み幅・最小数量など）を返却します。CCXT の `load_markets()` から得られるメタデータをインメモリ（1時間 TTL）で保持して高速に応答します。

TTL更新失敗時は最大24時間last-known-goodを返します。市場が解決できる場合はinactive/unknownやnullの制約値も返しますが、inactive市場への注文は422、必須の`min_qty`・`qty_step`が欠ける市場や不正な制約値を持つ市場への注文は503です（`min_notional`は市場に定義されている場合のみ検査されます）。

- **Path**: `GET /api/v1/instruments`
- **Query Parameters**:
  - `exchange_id` (str, 必須): e.g. `"bybit"`
  - `symbol` (str, 任意): e.g. `"BTCUSDT"` または `"BTC/USDT:USDT"`
- **Response Model**: `list[InstrumentView]`

**レスポンス例**:
```json
[
  {
    "exchange_id": "bybit",
    "symbol": "BTCUSDT",
    "base_asset": "BTC",
    "quote_asset": "USDT",
    "settle_asset": "USDT",
    "contract_type": "linear_perpetual",
    "contract_size": "1.0",
    "qty_step": "0.001",
    "min_qty": "0.001",
    "max_qty": "100.0",
    "price_step": "0.10",
    "min_notional": "5.0",
    "status": "active"
  }
]
```

---

### 5. リアルタイム価格照会 (`GET /api/v1/prices`)

未保有銘柄を含む任意の Universe 銘柄に対して、発注直前の参照価格（Mark Price, Last Price, 仲値等）を即時取得します。オンデマンド取得＋1秒 TTL キャッシュでレートリミットを保護します。

成功には板から算出した`mid_price`と`observed_at`が必須です。mark/last等は取得不能ならnullを許容します。価格cacheは期限後のstale値を使用せず、複数symbol指定はatomicに応答します。

- **Path**: `GET /api/v1/prices`
- **Query Parameters**:
  - `exchange_id` (str, 必須): e.g. `"bybit"`
  - `symbols` (str, 任意): カンマ区切りの銘柄リスト（例: `"BTCUSDT,ETHUSDT"`）
- **Response Model**: `list[PriceView]`

**レスポンス例**:
```json
[
  {
    "exchange_id": "bybit",
    "symbol": "BTCUSDT",
    "mark_price": "62150.50",
    "last_price": "62148.00",
    "bid_price": "62147.50",
    "ask_price": "62148.50",
    "mid_price": "62148.00",
    "observed_at": "2026-09-10T00:15:00.123456Z"
  }
]
```

---

### 6. 口座残高・余力照会 (`GET /api/v1/balance`)

設定ファイル（`initial_balance`）および既存の確定損益（手数料控除済純損益）・未実現損益から決定論的にオンデマンド算出した純資産（Equity）および余力を返却します（`total_fee` は監査・表示用内訳）。

Paper環境ではUSD・USDC・USDTを1:1として集計します。その他の決済通貨を持つopen position、または1件でも価格取得不能なpositionがある場合は503です。

- **Path**: `GET /api/v1/balance`
- **Response Model**: `BalanceView`

**レスポンス例**:
```json
{
  "currency": "USDT",
  "initial_balance": "10000.000000",
  "realized_pnl": "150.250000",
  "unrealized_pnl": "-25.100000",
  "total_fee": "12.400000",
  "equity": "10125.150000",
  "used_margin": "1250.000000",
  "available_balance": "8875.150000",
  "updated_at": "2026-09-10T00:15:00.123456Z"
}
```

---

### 7. 注文・約定履歴 (`GET /api/v1/history/orders`, `GET /api/v1/history/fills`)

過去の注文履歴を照会します。ページネーションおよびフィルタリングに対応しています。

注文・約定履歴のsymbol検索はDB上の監査記録を基準とし、現在の取引所設定や外部metadata APIへ通信せず、新しいアダプターも生成しません。未設定exchange・廃止済みsymbol・未知symbolは正常な検索条件として扱い、DBに入力表記と一致する記録がなければ`200 OK`で`{"items": [], "next_cursor": null}`を返します。入力表記そのものに加え、既にロード済みのalias cacheがある場合だけcanonical表記もbest-effortで検索し、新旧両方の保存表記を対象にします。前後空白を含むsymbolは形式違反として422です。

- **注文履歴のQuery Parameters**:
  - `from` / `to` (date, 任意): UTC日付の両端を含む期間。`from > to`または3660日を超える範囲は422
  - `exchange_id` (str, 任意)
  - `symbol` (str, 任意)
  - `side` (`buy` / `sell`, 任意)
  - `status` (str, 任意)
  - `strategy_id` (str, 任意): 特定戦略の注文のみを絞り込み
  - `cursor` (str, 任意): 直前レスポンスの`next_cursor`
  - `limit` (int, 任意, default: 100, range: 1〜200)

- **約定履歴のQuery Parameters**:
  - `from` / `to`、`exchange_id`、`symbol`、`side`、`cursor`、`limit`（意味と制約は注文履歴と同じ）

結果は時刻とIDの降順です。レスポンスは`items`と、不透明な継続トークン`next_cursor`を持ちます。最終ページでは`next_cursor`が`null`になります。クライアントはcursorを解析・生成せず、そのまま次のリクエストへ渡してください。

#### 直近約定一覧 (`GET /api/v1/fills`)

カーソルが不要な監視・画面表示向けに、`executed_at`降順の`FillView`配列を返します。`exchange_id`は任意、`limit`は既定100で、1未満は1、1000超は1000へ丸めます。安定したページングや期間・symbol・side検索が必要な場合は`GET /api/v1/history/fills`を使用してください。

---

### 8. ポジション全決済 (`POST /api/v1/positions/close`)

```json
{
  "request_id": "close-20260830-001",
  "exchange_id": "binance",
  "symbol": "BTC/USDT"
}
```

- 最新ポジションをロックし、反対売買の Reduce-only 成行注文を生成。
- `symbol` を省略すると指定取引所の全ポジションを一括決済。
- APIへ任意の`strategy_id`を指定可能。同じ`request_id`、取引所、canonical symbol、`strategy_id`の再送は既に生成済みの決済注文を再利用し、異なる操作へのキー再利用は409です。
- trade-ui経由の直接注文と全決済では、ブラウザpayloadの値にかかわらずproxyが`strategy_id: "manual"`を強制付与します。
- 決済対象の既存未完了注文には自動的に取消要求を設定。

---

### 9. 取引制御 (`POST /api/v1/close-only` / `POST /api/v1/kill-switch`)

```json
{
  "enabled": true,
  "reason": "emergency maintenance"
}
```

- **Close-only**: 有効化時、未完了の非 Reduce-only 注文へ取消要求を発行し、新規ポジションの注文を拒否。
- **Kill Switch**: Close-only を解除し、全注文の執行・受付を即時停止。
