# API 仕様

`trade-api` は HTTP REST API を提供します。`/health`, `/ready` 以外の全エンドポイントで `Authorization: Bearer <token>` が必須です。

この文書はAPI入力・応答・エラーの**将来の目標仕様**の正本です。既存エンドポイントの詳細化と契約変更、Bybit設定外銘柄、分割全決済、Kill Switchによる受付停止・取消を含みます。現行コードの振る舞いを併記しません。実装時にはBot・trade-ui・テストを同じ契約へ移行します。Bybit機能の概要・受入条件は [Bybit 動的銘柄対応](bybit-dynamic-symbols.md) を参照してください。

## エンドポイント一覧

| Method | Path | 用途 |
|---|---|---|
| `GET` | `/health` / `/ready` | ヘルスチェック（Paper環境属性付き） |
| `GET` | `/api/v1/exchanges` | 設定済み取引所・設定済み銘柄一覧 |
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
| `GET` | `/api/v1/close-requests/{request_id}` | 全決済親処理と全子注文の状態照会 |
| `POST` | `/api/v1/close-requests/{request_id}/cancel` | 全決済親処理の取消要求 |
| `GET` | `/api/v1/pnl` | 実現損益合計 |
| `GET` | `/api/v1/history/orders` | 注文履歴（`strategy_id` フィルタ・ページネーション対応） |
| `GET` | `/api/v1/history/fills` | 約定履歴 |
| `GET` | `/api/v1/history/pnl` | 日次損益履歴 |
| `GET` | `/api/v1/trading-control` | Close-only / Kill Switch 状態取得 |
| `POST` | `/api/v1/close-only` | Close-only 設定更新 |
| `POST` | `/api/v1/kill-switch` | Kill Switch 設定更新 |

## 共通規則

- Bybit では、取引所 metadata で USDT 建て・USDT 決済の Linear Perpetual と確認できれば、`settings.json` の `symbols` にない銘柄も明示指定できます。canonical symbol は `BTCUSDT` 形式の大文字市場 ID です。`BTC/USDT:USDT` 形式も入力でき、応答とDBには canonical symbol を使います。他取引所の設定済み銘柄の規則は維持します。比較はcase-sensitiveで、前後空白は `422 Unprocessable Entity` です。
- 銘柄を省略した市場情報APIと`GET /api/v1/exchanges`は設定済み銘柄を返します。Bybit の全取引可能銘柄一覧ではありません。履歴APIはDBの監査記録を検索し、照会時の設定や外部市場データに依存しません。
- 主なエラー区分は、認証失敗が`401`、対象なしが`404`、冪等性キーや取引制御との競合が`409`、注文・市場情報APIでの未知銘柄・対象外市場・取引不能・確定した制約違反が`422`、必要なmetadata・Mark Price・注文板・評価情報を取得できない場合が`503`です。DBローカルの履歴・保存済み建玉検索では未知銘柄は空結果とします。取得済みmetadataの検査に必要な制約が欠落・不正な場合も、利用者の入力違反ではなく`503`です。
- すべての金額・数量は精度を失わない10進数のJSON文字列、日時はUTCのISO 8601文字列で返します。`null`許容フィールドも省略せず返します。各節のレスポンスモデル名は共通のフィールド集合を表し、同じモデルを使うAPIではフィールドの意味を変えません。
- 以下に個別の例外がなければ、認証失敗は`401`、DBなど必須の内部依存が利用不能な場合は`503`です。Query・Path・Bodyの型・形式・範囲違反は`422`です。Bybit銘柄関連以外のエラーJSONはトップレベルの`detail`を持ちます。

Bybit の銘柄関連エラーはトップレベルの文字列`detail`（説明文）と`reason_code`（機械判定値）を返します。例:

```json
{"detail":"min_qty is missing for BTCUSDT","reason_code":"instrument_metadata_invalid"}
```

認証・対象なし・一般的な入力モデル検証エラーの`detail`は文字列または項目別エラーの配列です。BotはBybit銘柄関連エラーでのみトップレベルの`reason_code`を判定に使用します。

| Bybit のエラー | HTTP | `reason_code` |
|---|---:|---|
| 未知の銘柄 | 422 | `unknown_symbol` |
| 対象外の市場 | 422 | `unsupported_market` |
| 新規・増加注文ができない状態 | 422 | `instrument_not_tradable` |
| 必要な銘柄metadataの取得失敗 | 503 | `instrument_data_unavailable` |
| 取得済みmetadataの検査に必要な制約の欠落・不正 | 503 | `instrument_metadata_invalid` |
| Mark Priceの取得失敗 | 503 | `mark_price_unavailable` |
| 必要な注文板の取得失敗 | 503 | `order_book_unavailable` |

## エンドポイント詳細仕様

### 1. ヘルスチェック (`GET /health` / `GET /ready`)

認証不要でシステムの稼働状態および環境属性を確認できます。

`/health` はプロセスの生存、`/ready` はリクエストを安全に処理できる状態を表します。`/ready` はDB接続、設定ファイルの読み込み、設定にBybitがある場合の有効期限内のBybit metadataを必須条件とします。Bybit metadataのTTLは設定可能な300秒（既定値）です。更新に失敗して有効期限が切れたmetadataは利用せず、`/ready` は503へ戻ります。取得済みmetadataで特定銘柄の制約だけが不正な場合はサービス全体を503にせず、当該銘柄の注文で判定します。Binance・dYdXの障害もサービス全体のreadinessへ波及させません。

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

### 2. 設定済み取引所一覧 (`GET /api/v1/exchanges`)

現在の設定にある取引所と、銘柄省略時の既定一覧を返します。Bybitの`symbols`も設定済みの既定銘柄だけであり、明示指定可能な対象市場の全件ではありません。

- **Path**: `GET /api/v1/exchanges`
- **Query / Body**: なし
- **成功**: `200 OK`、`list[ExchangeView]`。`ExchangeView`は`exchange_id: str`、`adapter: "ccxt" | "dydx"`、`network: str`、`symbols: list[str]`を持ちます。設定順に返し、設定が空なら空配列です。
- **エラー**: 認証失敗`401`、設定を読み込めない場合`503`。

**レスポンス例**:

```json
[
  {
    "exchange_id": "bybit",
    "adapter": "ccxt",
    "network": "mainnet",
    "symbols": ["BTCUSDT", "ETHUSDT"]
  }
]
```

---

### 3. 注文受付 (`POST /api/v1/orders`)

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
- `request_id`: 全取引所の注文（全決済の子を含む）と全決済親処理に共通する一意な冪等性キー。既存注文の確認はmetadata検査より先に行います。同一キーかつ同一内容（`exchange_id`、canonical symbol、`side`、`order_type`、`quantity`、`limit_price`、`reduce_only`、`strategy_id`）の再送は、既存注文を`200 OK`で返します。内容が異なる場合や全決済親処理とのキー衝突は`409 Conflict`です。同じcanonical symbolを再送した場合は、外部metadata障害中でも既存注文を照合できます。
- `request_id` は `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` に制限。既存のslash入りlegacy IDは直接照合APIの対象外。
- `strategy_id` (`Optional[str]`): 発注元の Bot / 戦略を識別する監査タグ。
- `symbol`: Bybit ネイティブ表記（`BTCUSDT`）および CCXT 統一表記（`BTC/USDT:USDT`）の双方を受理し、Bybit の大文字市場 ID に正規化してDB保存・返却。対象の USDT Linear Perpetual なら設定への登録は不要です。
- `order_type`: `limit`（`limit_price` 必須）または `market`。
- **銘柄別数量バリデーション**: 受付時に以下の静的制約を同期検査し、違反時は `422 Unprocessable Entity` を返却（`paper-executor` でも執行直前に二重検査）。
  - 最小数量: `quantity >= min_qty`
  - 刻み幅: `(quantity - min_qty) % qty_step == 0`
  - 最小 Notional: `quantity * price >= min_notional`（市場メタデータに正の`min_notional`が定義されている場合のみ適用。未定義なら検査しません。Limit注文: `limit_price`、API受付時のMarket注文: 新しい注文板の仲値`mid_price`。Bybitの`inactive`/`unknown`市場に対する既存建玉のReduce-only Marketだけは、新しいMark Priceを使用）
  - Bybit Market注文は正の`max_market_qty`を必須とし、`quantity <= max_market_qty`を検査します。`max_qty`が定義されていればその上限も適用し、定義済みの値が不正なら503です。成行上限が欠落・不正な場合は503 `instrument_metadata_invalid`で、`max_qty`を代用しません。
- **取引状態と情報不足**: Bybit の新規・増加注文は`active`の対象市場に限ります。`inactive`または`unknown`は`422`です。既存建玉のReduce-only縮小・決済は両状態で認めますが、注文種別はMarketのみです。状態が執行前に変わった既存のReduce-only Limit注文には取消要求を設定します。受付時に実際に検査する市場metadata、Mark Price、注文板を取得できなければ、注文を作成せず理由コード付きの`503`を返します。
- **制約値の異常**: `min_qty`・`qty_step`の欠落/非正値、定義済み`min_notional`の非正値など、実際に検査する制約が不正なら`503 instrument_metadata_invalid`です。通常注文と部分決済の受付後に判明した場合、Executorは終端Rejectではなく再試行します。取得できた正常な制約に数量が違反するときだけ422です。
- **Reduce-only 特例**: `reduce_only=true`かつ注文数量が現在建玉の全量に一致する単一注文は、端数残留を防ぐため`qty_step`・`min_notional`・`min_qty`の制約を、値の欠落・不正時も含めて免除します。Bybit Market注文の`max_market_qty`と定義済み`max_qty`、正の数量、反転禁止は免除しません。単一注文が成行上限を超える場合は422で拒否し、分割が必要なら`POST /api/v1/positions/close`を使用します。

---

### 4. `request_id` 直接照合 (`GET /api/v1/orders/by-request-id/{request_id}`)

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

### 5. 注文IDによる照会・取消・約定明細

`order_id`は`OrderView.id`として返された不透明な文字列です。利用者はUUIDとして解析・再生成しません。存在しないIDは、形式に関係なく`404 Not Found`です。この節で使う`OrderView`の全フィールドとJSON例は前節の「`request_id`直接照合」と共通です。

#### 注文状態 (`GET /api/v1/orders/{order_id}`)

- **Path**: `order_id` (str, 必須): `OrderView.id`をそのまま指定。
- **Query / Body**: なし
- **成功**: `200 OK`、`OrderView`。`status`、`filled_quantity`、`cancellation_requested`、`retry_count`などを含めて現在の保存状態を返します。
- **エラー**: 認証失敗`401`、注文未存在`404`、DB利用不能`503`。

**レスポンス例**: 前節の`OrderView` JSON例と同じ構造です。注文IDだけを指定しても、`request_id`・監査タグ・執行状態を含む全フィールドを返します。

#### 取消要求 (`POST /api/v1/orders/{order_id}/cancel`)

- **Path**: `order_id` (str, 必須)
- **Query / Body**: なし
- **成功**: 非終端注文への初回・取消待ちへの再送は`202 Accepted`。`cancellation_requested=true`を保存した`OrderView`を返します。Executorによる取消完了後の再送は`200 OK`で`status=canceled`の`OrderView`を返します。`202`は取消完了を意味しません。状態確認にはGETを使います。
- **エラー**: 認証失敗`401`、注文未存在`404`、`filled`・`rejected`・`failed`など取消以外の終端状態は`409`、DB利用不能`503`。

取消受付後の新規約定は記録しません。約定が先に確定して注文が終端になっていれば取消は`409`です。約定保存時にも取消状態をトランザクション内で再確認します。全決済親処理の子注文を個別に取り消した場合は、後述の「全決済親処理の照会・取消」に従って親全体の取消へ進めます。

**`202 Accepted`応答の主要フィールド（`OrderView`からの抜粋）**:

```json
{
  "id": "c3b5f5c2-7e14-4bb7-b9d5-8351297d171f",
  "status": "open",
  "filled_quantity": "0",
  "cancellation_requested": true
}
```

#### 注文別約定 (`GET /api/v1/orders/{order_id}/fills`)

- **Path**: `order_id` (str, 必須)
- **Query**: `cursor` (str, 任意、不透明な継続トークン)、`limit` (int, 任意、既定100、範囲1〜200)
- **Body**: なし
- **成功**: `200 OK`、`FillPage`。`items: list[FillView]`と`next_cursor: str | null`を返します。約定は注文内の約定連番の昇順で、同じ連番は一度だけ返します。最終ページでは`next_cursor=null`です。注文に約定がなければ`{"items": [], "next_cursor": null}`を返します。
- **エラー**: 認証失敗`401`、注文未存在`404`、不正なcursor・別注文のcursor・範囲外のlimitは`422`、DB利用不能`503`。

`FillView`は`id`、`order_id`、`exchange_id`、`symbol`、`side`、`quantity`、`price`、`fee`、`liquidity_role`、`market_data_id`、`executed_at`を持ちます。cursorはクライアントが解析・生成せず、同じ注文IDへの次ページ要求へそのまま渡します。

**レスポンス例**:

```json
{
  "items": [{
    "id": "c754c75a-61d2-4ccc-96a1-232860baf96e",
    "order_id": "c3b5f5c2-7e14-4bb7-b9d5-8351297d171f",
    "exchange_id": "bybit",
    "symbol": "BTCUSDT",
    "side": "buy",
    "quantity": "0.004",
    "price": "62150.50",
    "fee": "0.136731",
    "liquidity_role": "taker",
    "market_data_id": "1538f6cd2a17d6cab201e967cd635ff66f8d4d5adadefcd4babe268d9c24d565",
    "executed_at": "2026-09-10T00:15:01.045123Z"
  }],
  "next_cursor": "opaque-next-page-token"
}
```

---

### 6. 銘柄メタデータ照会 (`GET /api/v1/instruments`)

発注前の端数処理やポートフォリオ丸めに必要な銘柄仕様（刻み幅・最小数量など）を返却します。Bybit の USDT Linear Perpetual は設定外でも明示指定でき、CCXT の `load_markets()` から得られるメタデータを`exchanges.bybit.metadata_ttl_seconds`（既定300秒）のインメモリキャッシュで利用します。`symbol`を省略した場合は設定済み銘柄だけを返します。

期限切れmetadataは返しません。Bybit が認識する対象市場なら`inactive`/`unknown`や`null`の制約値も`200 OK`で返します。`new_or_increase_allowed`は状態が`active`で、すべての注文種別に共通する必須制約が正常な場合だけ`true`です。`false`の理由は`new_or_increase_reason_code`で返します。これは市場metadataから分かる基本的な適格性であり、Market固有の`max_market_qty`、注文板・Mark Priceの取得、個別リスク検査の成功まで保証しません。未知銘柄・対象外市場は422、metadata取得失敗は503です。

`inactive`/`unknown`なら`new_or_increase_reason_code=instrument_not_tradable`、取引状態が`active`で検査に必要な`min_qty`・`qty_step`または定義済み`min_notional`が不正なら`instrument_metadata_invalid`です。状態と制約の両方に問題がある場合は状態の理由を優先します。いずれも新規・増加の基本的な適格性だけを示し、保有建玉のReduce-only全決済可否は別途判定します。

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
    "max_market_qty": "50.0",
    "price_step": "0.10",
    "min_notional": "5.0",
    "status": "active",
    "new_or_increase_allowed": true,
    "new_or_increase_reason_code": null
  }
]
```

---

### 7. リアルタイム価格照会 (`GET /api/v1/prices`)

未保有銘柄を含む Bybit の対象銘柄に対して、設定への登録なしで Mark Price と取得時刻を照会できます。`symbols`を省略した場合は設定済み銘柄だけを返します。Mark Price は照会ごとに取得します。

Bybit の価格照会が成功するには正の`mark_price`と`mark_observed_at`が必須です。`mark_observed_at`はサーバーがMark Priceを取得したUTC時刻です。注文板を取得できない場合もMark Priceがあれば成功し、板由来の`bid_price`、`ask_price`、`mid_price`、`observed_at`はnullを許容します。`observed_at`は取引所の注文板timestampを優先し、存在しなければサーバーの受信時刻です。取得失敗時に古い価格を返さず、複数symbol指定は全件成功時だけ応答します。Bybit 以外では板由来の`mid_price`と`observed_at`を必須とします。

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
    "mark_observed_at": "2026-09-10T00:15:00.123456Z",
    "last_price": "62148.00",
    "bid_price": "62147.50",
    "ask_price": "62148.50",
    "mid_price": "62148.00",
    "observed_at": "2026-09-10T00:15:00.123456Z"
  }
]
```

---

### 8. 口座残高・余力照会 (`GET /api/v1/balance`)

設定ファイル（`initial_balance`）および既存の確定損益（手数料控除済純損益）・未実現損益から決定論的にオンデマンド算出した純資産（Equity）および余力を返却します（`total_fee` は監査・表示用内訳）。

Paper環境ではUSD・USDC・USDTを1:1として集計します。Bybit の設定外建玉もその銘柄metadataとMark Priceから評価します。その他の決済通貨を持つopen position、または1件でも必要なmetadata・価格を取得できないpositionがある場合は503です。

使用証拠金は全銘柄に設定ファイルの`account.default_leverage`を一律適用し、銘柄別レバレッジは持ちません。計算式とBybitのMark Priceによる評価単価は[取引・執行仕様](trading-engine.md)に従います。

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

### 9. 建玉照会 (`GET /api/v1/positions`, `GET /api/v1/current-positions`)

#### 保存済み建玉 (`GET /api/v1/positions`)

DBに保存された建玉行を返します。数量ゼロになった行も、累計確定損益の監査記録として含めます。現在の取引所設定・市場状態・外部市場データには依存しません。

- **Path**: `GET /api/v1/positions`
- **Query**: `exchange_id` (str, 任意)、`symbol` (str, 任意)。両方省略すれば全保存行を返します。`symbol`はDBの保存表記に完全一致させます。大文字`BASE/USDT:USDT`（`BASE`は英数字1文字以上）ならDBローカルで`BASEUSDT`へも変換し、Bybitのcanonical symbolに照合します。`exchange_id`省略時は全取引所の完全一致とBybitの変換後の一致を併せて検索します。アダプター生成やmetadata取得は行いません。
- **Body**: なし
- **成功**: `200 OK`、`list[PositionView]`。`PositionView`は`exchange_id`、canonical `symbol`、符号付き`quantity`、`average_entry_price`、手数料控除後の累計`realized_pnl`、`updated_at`を持ちます。`exchange_id`・`symbol`の昇順で返し、該当行がなければ空配列です。未知の取引所・銘柄も正しい検索形式なら空配列です。
- **エラー**: 認証失敗`401`、前後空白など検索形式の違反`422`、DB利用不能`503`。

**レスポンス例**:

```json
[
  {
    "exchange_id": "bybit",
    "symbol": "BTCUSDT",
    "quantity": "0",
    "average_entry_price": "0",
    "realized_pnl": "12.300000",
    "updated_at": "2026-09-10T00:15:00.123456Z"
  }
]
```

#### 現在建玉 (`GET /api/v1/current-positions`)

非ゼロの保存済み建玉だけを価格評価して返します。Bybitでは新しいMark Price、他取引所では新しい注文板仲値を使います。保存済み建玉は設定一覧や現在の取引状態から消えません。設定から削除した取引所に建玉行が残る場合も、その取引所を指定して照会できます。

- **Path**: `GET /api/v1/current-positions`
- **Query**: `exchange_id` (str, 必須)、`symbol` (str, 任意)。symbolの照合は前項と同じDBローカル規則です。設定済み、またはDBに保存行がある取引所を受け付けます。設定にもDBにも存在しない取引所は`422`です。形式が正しいが該当建玉のないsymbolは空の集計を返します。
- **Body**: なし
- **成功**: `200 OK`、`CurrentPositionsView`。集計フィールドは`exchange_id`、応答を組み立てたUTC時刻`refreshed_at`、`valuation_complete`、`unpriced_count`、`total_unrealized_pnl`、`positions: list[CurrentPositionView]`です。建玉がなければ`positions=[]`、`valuation_complete=true`、`unpriced_count=0`、`total_unrealized_pnl="0"`です。
- **エラー**: 認証失敗`401`、必須Queryの欠落・形式違反・設定にもDBにもない取引所は`422`、DB利用不能`503`。個々の市場情報不足だけを理由にレスポンス全体を503にはしません。

`CurrentPositionView`は`exchange_id`、canonical `symbol`、`base_asset`、`quote_asset`、`position_side`（`buy`/`sell`）、符号付き`quantity`、`average_entry_price`、`current_price`、`unrealized_pnl`、`position_updated_at`、`price_observed_at`、`valuation_status`（`ok`/`unavailable`）、`valuation_reason_code`、`valuation_detail`を持ちます。資産名をmetadataから確定できないときは`base_asset`・`quote_asset`を`null`にし、symbolから推測しません。

価格評価できない建玉も数量と平均取得価格を返し、価格・価格時刻・未実現損益を`null`にします。`valuation_status=unavailable`の建玉数が`unpriced_count`です。1件でもあれば`valuation_complete=false`、`total_unrealized_pnl=null`とし、評価可能な銘柄だけの小計を全体合計として返しません。評価できた建玉は理由コード・説明文をともに`null`にします。`price_observed_at`は各市場価格の取得・観測時刻で、`refreshed_at`とは別です。

| `valuation_reason_code` | 意味 |
|---|---|
| `exchange_not_configured` | 保存行の取引所が現在の設定にない |
| `instrument_data_unavailable` | 評価に必要な銘柄metadataを取得できない |
| `instrument_metadata_invalid` | 評価に必要な取得済みmetadataが不正 |
| `mark_price_unavailable` | Bybitの新しいMark Priceを取得できない |
| `order_book_unavailable` | 他取引所の新しい注文板仲値を取得できない |
| `unsupported_settlement_currency` | Paper口座で評価できない決済通貨 |

**一部の建玉を評価できない場合のレスポンス例**:

```json
{
  "exchange_id": "bybit",
  "refreshed_at": "2026-09-10T00:15:02.000000Z",
  "valuation_complete": false,
  "unpriced_count": 1,
  "total_unrealized_pnl": null,
  "positions": [
    {
      "exchange_id": "bybit",
      "symbol": "BTCUSDT",
      "base_asset": "BTC",
      "quote_asset": "USDT",
      "position_side": "buy",
      "quantity": "0.01",
      "average_entry_price": "60000",
      "current_price": "62150",
      "unrealized_pnl": "21.50",
      "position_updated_at": "2026-09-10T00:14:00.000000Z",
      "price_observed_at": "2026-09-10T00:15:01.000000Z",
      "valuation_status": "ok",
      "valuation_reason_code": null,
      "valuation_detail": null
    },
    {
      "exchange_id": "bybit",
      "symbol": "ETHUSDT",
      "base_asset": "ETH",
      "quote_asset": "USDT",
      "position_side": "sell",
      "quantity": "-1",
      "average_entry_price": "2000",
      "current_price": null,
      "unrealized_pnl": null,
      "position_updated_at": "2026-09-10T00:14:30.000000Z",
      "price_observed_at": null,
      "valuation_status": "unavailable",
      "valuation_reason_code": "mark_price_unavailable",
      "valuation_detail": "fresh Mark Price is unavailable"
    }
  ]
}
```

---

### 10. 注文・約定履歴 (`GET /api/v1/history/orders`, `GET /api/v1/history/fills`)

過去の注文履歴を照会します。ページネーションおよびフィルタリングに対応しています。

注文・約定履歴のsymbol検索はDB上の監査記録を基準とし、現在の取引所設定や外部metadata APIへ通信せず、新しいアダプターも生成しません。未設定exchange・廃止済みsymbol・未知symbolは正常な検索条件として扱い、該当記録がなければ`200 OK`で`{"items": [], "next_cursor": null}`を返します。Bybit の canonical symbol は上場廃止後も検索できます。大文字`BASE/USDT:USDT`（`BASE`は英数字1文字以上）は`BASEUSDT`へDBローカルで変換します（例: `BTC/USDT:USDT`→`BTCUSDT`）。入力表記そのものと変換後の表記を照合し、形式外は機械変換しません。前後空白を含むsymbolは形式違反として422です。保存不変条件は[DB運用手順](database.md)を参照してください。

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

### 11. 損益合計・日次損益履歴

損益は取引所・銘柄をまたぐ単一Paper口座の値です。約定の手数料は新規建て時も含めて確定損益から差し引き、日次損益は各約定の`executed_at`をUTCに変換した日付へ計上します。後日記録した約定も記録日ではなく約定日に反映します。`/pnl`と`/history/pnl`は同じ日次損益台帳を正本として使い、`/balance`の確定損益と一致します。

#### 確定損益合計 (`GET /api/v1/pnl`)

- **Path**: `GET /api/v1/pnl`
- **Query / Body**: なし。全取引所・全期間が対象で、取引所・銘柄によるフィルタは設けません。
- **成功**: `200 OK`、`PnlView`。`currency`は`account.currency`、`realized_pnl`は日次損益台帳の全期間合計です。記録がなければ`"0"`を返します。未実現損益は含めず、必要なら`/balance`または`/current-positions`で照会します。
- **エラー**: 認証失敗`401`、設定またはDB利用不能`503`。

**レスポンス例**:

```json
{
  "currency": "USDT",
  "realized_pnl": "150.250000"
}
```

#### 日次損益履歴 (`GET /api/v1/history/pnl`)

- **Path**: `GET /api/v1/history/pnl`
- **Query**: `from` / `to` (UTC日付`YYYY-MM-DD`、ともに任意、両端を含む)。両方省略するとサーバーの今日を`to`とする直近365日、`from`だけなら`to=今日`、`to`だけなら`from=to-364日`です。`cursor`と`limit`はありません。
- **Body**: なし
- **成功**: `200 OK`、`PnlHistoryView`。`timezone="UTC"`、実際に使った`from_date`・`to_date`、日付昇順の`points: list[PnlHistoryPoint]`を返します。各点は`trade_date`、その日の手数料控除後`daily_realized_pnl`、その日までの**取引開始以来の**`cumulative_realized_pnl`を持ちます。指定範囲より前の損益も累計の初期値に含めます。取引のない日も日次値`"0"`で返し、台帳が空でも指定・既定範囲の全日をゼロで返します。今日の点は照会時点までに確定した約定だけを含みます。
- **エラー**: 認証失敗`401`、日付形式不正・未来日・`from > to`・両端を含む日数が3660日を超える場合は`422`、DB利用不能`503`。

**レスポンス例**（9月9日より前の通算確定損益が`100`）:

```json
{
  "timezone": "UTC",
  "from_date": "2026-09-09",
  "to_date": "2026-09-10",
  "points": [
    {
      "trade_date": "2026-09-09",
      "daily_realized_pnl": "5.00",
      "cumulative_realized_pnl": "105.00"
    },
    {
      "trade_date": "2026-09-10",
      "daily_realized_pnl": "0",
      "cumulative_realized_pnl": "105.00"
    }
  ]
}
```

---

### 12. ポジション全決済 (`POST /api/v1/positions/close`)

指定取引所の保有建玉のスナップショットに対する、非同期の全決済親処理を登録します。`202 Accepted`は決済注文群の登録を意味し、建玉ゼロの保証ではありません。子注文と残数量は親処理のGETで監視します。

```json
{
  "request_id": "close-20260830-001",
  "exchange_id": "bybit",
  "symbol": "BTCUSDT",
  "strategy_id": "alpha-v1"
}
```

- **Body**: `request_id`（通常注文と共通の一意キー、注文APIと同じ形式）、`exchange_id`（必須）、`symbol`（任意）、`strategy_id`（任意）。`symbol`省略と明示指定は別の操作内容です。
- `symbol`省略時は受付時点で指定取引所に存在する全建玉を対象とし、後からできた建玉は含めません。対象建玉をロックし、全対象の市場・価格・上限を事前検査します。1銘柄でも受付に必要な情報が欠ければ、親処理も子注文も作らず503を返します。対象外市場と確定した場合は422で、同様に何も作成しません。
- 対象建玉の既存未完了注文には取消要求を設定し、反対売買のReduce-only Market子注文を作ります。親処理、対象建玉、取消要求、初期子注文、冪等性キーは1トランザクションで記録します。同じ建玉で別の全決済が進行中なら409です。進行中の対象銘柄への別の`POST /orders`も、Reduce-onlyを含め409です。
- Bybit Market子注文には正の`max_market_qty`が必須です。`max_qty`があれば両者の小さい方を子注文の上限とし、Decimalで数量合計が受付時の建玉全量に一致するよう分割します。`max_market_qty`が欠落・不正、または定義済み`max_qty`が不正なら`503 instrument_metadata_invalid`で、`max_qty`による成行上限の代用はしません。
- 親に属する**すべて**の子注文は`min_qty`、`qty_step`、`min_notional`を値の欠落・不正時も含めて免除します。内部の`max_order_quantity`、`max_order_notional`、注文頻度、`max_position_notional`、日次損失上限も免除します。市場の`max_market_qty`と定義済み`max_qty`、正の数量、対象市場判定、Reduce-onlyの反転禁止、必要データの鮮度は免除しません。
- 初期子注文を受付時に全件作成し、同じ建玉では子順序で逐次執行します。部分約定で残りが取り消された場合は実際の建玉を再計算し、同じ親に追加子注文を作ります。受付済み子注文の数量は変更しません。流動性や一時的な市場情報が不足した場合、固定回数で終了せず、指数バックオフで待機します。
- Bybitの`inactive`/`unknown`対象市場で既存建玉を閉じるときは、新しいMark Priceを取得できれば板がなくてもPaper成行決済します。受付時に必要なMark Priceを取得できなければ503です。受付後の取得失敗は親を`waiting`として再試行します。対象外市場と確定した銘柄はその建玉の処理を`failed`にしますが、他銘柄の執行は続けます。
- trade-ui経由の直接注文と全決済では、ブラウザpayloadの値にかかわらずproxyが`strategy_id: "manual"`を強制付与します。

**HTTP応答**: 新規の非空処理は`202 Accepted`。初回に対象建玉がなければ子注文0件の`completed`親処理を記録して`200 OK`。同一`request_id`・同一入力の再送は市場情報を再取得せず、その時点の親処理を`200 OK`で返します。キーを他の通常注文や異なる全決済入力に使った場合は409です。

**レスポンスモデル `CloseRequestView`**: `request_id`、`exchange_id`、`symbol`（省略時は`null`）、`strategy_id`、`status`、`positions: list[ClosePositionView]`、`items: list[OrderView]`、`reason_code`、`detail`、`created_at`、`updated_at`。`ClosePositionView`は`symbol`、受付時と現在の符号付き建玉数量（`initial_position_quantity`、`remaining_position_quantity`）、`status`、`reason_code`、`detail`を持ちます。`items`は初期子注文と後から追加した子注文を含み、銘柄・子順序で安定して並びます。子の`request_id`は親の内部ID・建玉ID・連番から一意に生成する不透明な値で、利用者は解析しません。

親の`status`は`queued`、`running`、`waiting`、`canceling`、`completed`、`canceled`、`failed`です。`completed`は対象建玉がすべてゼロで、残る子注文も終端のときだけです。1銘柄が確定失敗しても他銘柄の処理中は親を`running`または`waiting`とし、個別の失敗を`positions`に表示します。すべての処理が終わったときに失敗が残れば親を`failed`にします。

**`202 Accepted` レスポンス例（建玉数量20）**:

```json
{
  "request_id": "close-20260830-001",
  "exchange_id": "bybit",
  "symbol": "BTCUSDT",
  "strategy_id": "alpha-v1",
  "status": "queued",
  "positions": [{
    "symbol": "BTCUSDT",
    "initial_position_quantity": "20",
    "remaining_position_quantity": "20",
    "status": "queued",
    "reason_code": null,
    "detail": null
  }],
  "items": [{
    "id": "c3b5f5c2-7e14-4bb7-b9d5-8351297d171f",
    "request_id": "close-child-opaque-001",
    "strategy_id": "alpha-v1",
    "exchange_id": "bybit",
    "exchange_network": "mainnet",
    "symbol": "BTCUSDT",
    "side": "sell",
    "order_type": "market",
    "quantity": "20",
    "limit_price": null,
    "status": "pending",
    "rejection_reason": null,
    "reduce_only": true,
    "filled_quantity": "0",
    "average_fill_price": null,
    "total_fee": "0",
    "cancellation_requested": false,
    "resting_since": null,
    "last_market_data_id": null,
    "retry_count": 0,
    "next_attempt_at": "2026-09-10T00:15:00.123456Z",
    "created_at": "2026-09-10T00:15:00.123456Z",
    "updated_at": "2026-09-10T00:15:00.123456Z"
  }],
  "reason_code": null,
  "detail": null,
  "created_at": "2026-09-10T00:15:00.123456Z",
  "updated_at": "2026-09-10T00:15:00.123456Z"
}
```

### 13. 全決済親処理の照会・取消

- `GET /api/v1/close-requests/{request_id}`: 既存の親処理を`200 OK`の`CloseRequestView`で返し、未存在は404です。ポーリングではこのGETを使います。通常注文の`GET /api/v1/orders/by-request-id/{request_id}`は子注文を含む単一注文専用です。
- `POST /api/v1/close-requests/{request_id}/cancel`: 親を`canceling`にして未完了子注文へ取消要求を出し、`202 Accepted`の`CloseRequestView`を返します。全子注文の終端後に親を`canceled`にします。`canceled`への再送は`200 OK`で同じ親を返し、`completed`/`failed`には409、未存在は404です。
- `POST /api/v1/orders/{order_id}/cancel`で親に属する子注文を個別に取り消した場合も、親全体を`canceling`にして代替子注文の自動生成を止めます。取消受付後の約定記録を防ぐため、約定保存時に取消とKill Switchを再確認します。

---

### 14. 取引制御 (`GET /api/v1/trading-control` / `POST /api/v1/close-only` / `POST /api/v1/kill-switch`)

#### 現在の制御状態 (`GET /api/v1/trading-control`)

- **Path**: `GET /api/v1/trading-control`
- **Query / Body**: なし
- **成功**: `200 OK`、`TradingControlView`。`kill_switch: bool`、`close_only: bool`、`reason: str | null`、`updated_at: datetime`を返します。両フラグは同時に`true`になりません。`reason`は現在有効な制御の理由で、両方無効なら`null`です。`updated_at`は制御状態または有効理由を最後に変更したUTC時刻です。操作時だけ意味を持つ取消件数はGETに含めません。
- **エラー**: 認証失敗`401`、DB利用不能`503`。

**レスポンス例**:

```json
{
  "kill_switch": false,
  "close_only": true,
  "reason": "manual maintenance",
  "updated_at": "2026-09-10T00:15:00.123456Z"
}
```

#### 制御状態の更新 (`POST /api/v1/close-only` / `POST /api/v1/kill-switch`)

- **Body**: `enabled` (bool、省略時`true`)、`reason` (str | null、任意、最大500文字)。Queryはありません。
- **成功**: `200 OK`、`TradingControlUpdateView`。`TradingControlView`の全フィールドに、当該操作で新しく`cancellation_requested=true`にした注文の件数`cancellation_requested_count: int`を加えます。全決済の親処理自体は件数に含めません。同じ状態・理由への再送で追加の取消要求がなければ件数は0で、`updated_at`も変えません。
- **エラー**: 認証失敗`401`、Body形式違反`422`、DB利用不能`503`。

**リクエスト例**:

```json
{
  "enabled": true,
  "reason": "emergency maintenance"
}
```

- **Close-only**: 有効化時にKill Switchを解除し、未完了の非 Reduce-only 注文へ取消要求を発行して新規・増加注文を拒否します。有効なReduce-only決済は続けます。
- **Kill Switch**: 有効化時にClose-onlyを解除し、新規注文・全決済の受付を409で拒否します。未完了注文は取消へ進め、進行中の全決済親処理は`canceling`へ移します。約定保存時にフラグを再確認し、有効化の受付後に新たな約定を記録しません。既存注文を保留して解除後に再開することはありません。
- **解除**: 対象フラグを`false`にして他方の有効なフラグは変更しません。両方無効になれば`reason=null`です。解除済み注文と全決済親処理は自動再開しません。注文照会・取消APIはKill Switch有効時も使えます。

**`POST /api/v1/kill-switch`のレスポンス例**:

```json
{
  "kill_switch": true,
  "close_only": false,
  "reason": "emergency maintenance",
  "updated_at": "2026-09-10T00:15:00.123456Z",
  "cancellation_requested_count": 3
}
```
