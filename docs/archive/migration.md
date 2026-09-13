# Trade Container 移行・改修計画 (Migration to Gate Compliance)

> [!IMPORTANT]
> **アーカイブ済み・実装完了** — 本文は2026-09-09の実機受入調査で確認したギャップと合意仕様を記録したものです。2026-09-11に実装と自動検証を完了しました。現在の正本は[API仕様](../api.md)、[取引・執行仕様](../trading-engine.md)、[システム設計](../architecture.md)、[DB運用手順](../database.md)です。

本ドキュメントは、`crypto_alpha_platform` の「Gate: Trade_Container 実機受入調査」において検出された機能ギャップを解消し、Stage 5（Portfolio構築・端数処理）および Stage 6（Paper成行発注・照合）の実機接続要件を満たすために Trade_Container に必要な機能追加・改修仕様を定めたものです。

Trade Container の設計思想（**「外部キャッシュや不要なブローカーを持たず、公開データを用いてステートレスに約定シミュレーションを行う、軽量・堅牢なコンテナ基盤」**）を維持しつつ、過剰設計を避けた現実的な落としどころ（合意仕様）に基づいて構成されています。

---

## 1. 背景と改修目的

2026-09-09 に実施された `crypto_alpha_platform` 側の実機受入調査において、当時の Trade_Container は以下の理由により Gate 判定が **FAIL** となりました。

1. **専用インスタンス性の欠落**: 手動UI（`trade-ui`）からの注文や建玉が混在し、主体・戦略の分離機構が存在しない。
2. **Bybit USDT無期限契約の完全な未対応**: コードベース・設定ともに Bybit アダプターが存在せず、呼出が 422 で拒否される。
3. **必須 API の欠落**:
   - 口座残高（Cash / Equity / Margin）照会 API が存在しない。
   - 任意銘柄のリアルタイム参照価格（Mark/Last Price）照会 API が存在しない（既存建玉のある銘柄の仲値のみ）。
4. **数量仕様（刻み・最小値）の未定義**:
   - `qty_step`、`min_qty`、最小 notional 等の銘柄メタデータを返却・検査する機構がない。

本改修では、これらの課題を Trade Container 本来のシンプルさを損なわずに解決し、`alpha_runtime` が安全かつ決定論的に発注・照合を行える環境を整備します。

---

## 2. 改修項目一覧 (Feature Matrix)

| ID | 改修項目 | 分類 | 決定方針・落としどころ | 優先度 |
| :--- | :--- | :--- | :--- | :--- |
| **TC-D1** | CCXT拡張による Bybit USDT無期限対応 | アダプター | 独自アダプター新設は避け、`CcxtAdapter` に options 渡し機構を追加。シンボル表記揺れ（`BTCUSDT` ⇔ `BTC/USDT:USDT`）を透過的正規化 | **必須 (P0)** |
| **TC-D2** | 取引所・口座設定の拡張 | 設定 | `settings.json` に `bybit`（options 含む）および初期仮想口座設定（`account`）を追加 | **必須 (P0)** |
| **TC-D3** | 銘柄メタデータ照会 API の新設 | API/キャッシュ | `GET /api/v1/instruments`。DBテーブルは新設せず、CCXT `load_markets()` のメタデータをインメモリ（1時間TTL）保持して返却 | **必須 (P0)** |
| **TC-D4** | リアルタイム価格照会 API の新設 | API/キャッシュ | `GET /api/v1/prices`。未保有銘柄を含む Mark Price, Last Price, 仲値をオンデマンド（1秒TTLキャッシュ）取得して即時返却 | **必須 (P0)** |
| **TC-D5** | 口座残高・余力照会 API の新設 | API/モデル | `GET /api/v1/balance`。`accounts` テーブルは新設せず、設定ファイル＋既存の実現/未実現損益から決定論的にオンデマンド算出する参照専用API | **必須 (P0)** |
| **TC-D6** | 注文主体の監査タグと運用分離 | モデル/API/運用 | 複合キーによるDBマルチテナント化は見送り、`orders` テーブルに監査用 `strategy_id` カラムを追加。単一スタック（`compose.yaml`）で運用しUIは緊急用とする | **必須 (P0)** |
| **TC-D7** | 銘柄別数量制約バリデーション | 執行/リスク | API受付時同期検査（422）＋ Executor検査（Reject）の二重防壁。**Reduce-only 特例**（全決済時の端数免除）を導入 | **必須 (P0)** |
| **TC-D8** | `request_id` 直接照合 API の追加 | API | `GET /api/v1/orders/by-request-id/{request_id}` によるユニークインデックスを活用した直接注文照合 | **推奨 (P1)** |
| **TC-D9** | Paper 環境属性の明示的付与 | API/監査 | `/health` および `/ready` レスポンスに `environment: "paper"`, `simulation: true` を明示 | **推奨 (P1)** |

---

## 3. 各機能の詳細仕様

### TC-D1: CCXT拡張による Bybit USDT無期限対応 (`CcxtAdapter`)

#### 概要
Trade Container の「CCXT対応取引所はCCXTで共通化する」方針に従い、独自アダプターの新設（車輪の再発明）は行わず、既存の `CcxtAdapter` を拡張して取引所固有オプション（`options: {"defaultType": "linear"}` 等）を渡せるようにします。
また、API呼出側が Bybit ネイティブ表記（`BTCUSDT`）またはCCXT統一表記（`BTC/USDT:USDT`）を指定しても、設定上のcanonical表記（`BTCUSDT`）へ自動解決する透過的正規化層を配備します。CCXT統一表記への変換は外部API呼び出し境界だけで行います。

#### 実装場所
* 修正: `docker/share/copy/trade_common/exchange_adapters/ccxt_adapter.py`
* 修正: `docker/share/copy/trade_common/exchange_adapters/factory.py`
* 修正: `docker/share/copy/trade_common/config.py`

#### 仕様
```python
class CcxtAdapter:
    def __init__(self, config: ExchangeSettings):
        exchange_class = getattr(ccxt, config.exchange_id, None)
        if exchange_class is None:
            raise RuntimeError(f"unsupported CCXT exchange: {config.exchange_id}")
        self.exchange_id = config.exchange_id
        
        # options (e.g. defaultType: linear/swap) を CCXT クライアントへ伝達
        client_params = {"enableRateLimit": True}
        if config.options:
            client_params["options"] = config.options
        self.client = exchange_class(client_params)
        self.markets = self.client.load_markets()
        self._build_symbol_alias_map()

    def resolve_symbol(self, raw_symbol: str) -> str:
        """BTCUSDT または BTC/USDT:USDT を設定上のcanonical表記へ解決"""
        ...
```

---

### TC-D2: 取引所・口座設定の拡張 (`settings.json`)

#### 概要
`settings.json` の `exchanges` に `bybit` を追加し、さらに仮想ペーパー口座の初期設定（`account`）を新設します。

#### 設定例
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
  },
  "poll_interval_seconds": 1.0,
  "market_data_max_age_seconds": 10.0,
  "risk": {
    "max_order_quantity": "10.0",
    "max_order_notional": "50000.0",
    "max_position_notional": "100000.0",
    "max_daily_loss": "5000.0",
    "max_price_deviation_pct": "0.05",
    "max_orders_per_minute": 60
  }
}
```

---

### TC-D3: 銘柄メタデータ照会 API (`GET /api/v1/instruments`)

#### 概要
Stage 5（端数処理・ポートフォリオ丸め）に必要な銘柄仕様を返却します。
データベース（PostgreSQL）にテーブルは新設せず、CCXT アダプターがメモリ上に保持するマーケット情報（`markets[symbol]['precision']`, `markets[symbol]['limits']`）を整形して高速に返却します。

#### キャッシュ方針
* サーバー起動時にBybit `load_markets()`を実行し、1時間のインメモリTTLキャッシュを保持。更新失敗時は最大24時間last-known-goodを利用し、以後は該当APIおよびBybit readinessを503とする。

#### Endpoint 仕様
* **Path**: `GET /api/v1/instruments`
* **Query Parameters**:
  * `exchange_id` (str, 必須): e.g. `"bybit"`
  * `symbol` (str, 任意): e.g. `"BTCUSDT"` または `"BTC/USDT:USDT"`
* **Response Model**: `list[InstrumentView]`
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

### TC-D4: リアルタイム価格照会 API (`GET /api/v1/prices`)

#### 概要
未保有銘柄を含む任意の Universe 銘柄に対して、発注直前の参照価格（Mark Price, Last Price, 仲値）を即時取得できるエンドポイントを新設します。
DB永続化や常駐ポーリングスレッドは持たず、アダプター経由のオンデマンド取得＋1秒間の短時間 TTL キャッシュでレートリミットを保護します。
期限切れ価格は再利用せず、複数symbol指定は全件成功時のみ200を返します。

#### Endpoint 仕様
* **Path**: `GET /api/v1/prices`
* **Query Parameters**:
  * `exchange_id` (str, 必須): e.g. `"bybit"`
  * `symbols` (str, 任意): カンマ区切りの銘柄リスト (例: `"BTCUSDT,ETHUSDT"`)
* **Response Model**: `list[PriceView]`
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

### TC-D5: 口座残高・余力照会 API (`GET /api/v1/balance`)

#### 概要
複雑な清算エンジンや `accounts` テーブルの追加は行わず、`settings.json` の `initial_balance`、既存の `daily_pnl`（確定損益）、`positions`（未実現損益）、`fills`（手数料）から決定論的にオンデマンド算出する参照専用 API です。
発注時の動的余力チェックは行わず、Stage 5 側の判断に委ねます（Trade Container は静的なリスクリミットで防御）。

#### 計算ロジック
* $\text{Realized PnL} = \sum(\text{daily\_pnl.realized\_pnl}) \quad \text{（約定手数料控除済の確定純損益）}$
* $\text{Total Fee} = \sum(\text{fills.fee}) \quad \text{（監査・表示用内訳）}$
* $\text{Unrealized PnL} = \sum(\text{position.quantity} \times (\text{current\_mid\_price} - \text{position.average\_entry\_price}))$
* $\text{Equity} = \text{initial\_balance} + \text{Realized PnL} + \text{Unrealized PnL}$
* $\text{Used Margin} = \sum(|\text{position.quantity}| \times \text{current\_mid\_price} / \text{default\_leverage})$
* $\text{Available Balance} = \max(0, \text{Equity} - \text{Used Margin})$

#### Endpoint 仕様
* **Path**: `GET /api/v1/balance`
* **Response Model**: `BalanceView`
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

### TC-D6: 注文主体の監査タグと運用分離 (`strategy_id`)

#### 概要
`positions` や取引制御（Kill Switch 等）の複合キー化によるマルチテナント化は見送り、Trade Container は単一スタック（`compose.yaml`、ポート8000）で Bot 専用運用します。
`trade-ui` は緊急時の決済・確認用とし、Bot稼働中は手動発注を行わない運用とします。
一方で、注文の追跡・監査を可能にするため、`orders` テーブルに NULL 許容の `strategy_id` カラムを追加します。

#### 改修仕様
1. **DBマイグレーション**:
   - `orders` テーブルに `strategy_id VARCHAR(64) NULL` を追加。
   - インデックス `ix_orders_strategy_id` を作成。
2. **API 拡張**:
   - `POST /api/v1/orders` のリクエストモデルに `strategy_id: Optional[str]` を追加。
   - `GET /api/v1/history/orders` に `strategy_id` クエリパラメータを追加し、指定戦略の注文履歴のみを絞り込み可能にする。

---

### TC-D7: 銘柄別数量制約バリデーション (`risk.py` & API)

#### 概要
静的な上限値だけでなく、銘柄ごとの最小数量（`min_qty`）、刻み幅（`qty_step`）、最小 notional を検査します。
呼び出し元が即時検知できるよう `trade-api` で同期検査（422）を行い、さらに `paper-executor` の `risk.py` でも執行前に二重検査します。

#### 検査ルール
1. **最小数量**: `quantity >= instrument.min_qty`（違反時: 422 / Reject）
2. **刻み幅**: `(quantity - min_qty) % qty_step == 0`（違反時: 422 / Reject）
3. **最小 Notional**: `quantity * price >= instrument.min_notional`（違反時: 422 / Reject）
   - **判定価格 `price` の定義**:
     - **Limit 注文**: 注文パラメータの `limit_price` を使用（API 受付・Executor 執行前共通）。
     - **Market 注文**:
       - `trade-api` 同期検査: 直近の仲値 `mid_price`（TC-D4 のキャッシュまたはオンデマンド取得値）を使用。市場データ取得不能時は同期検査をバイパスし Executor へ委ねる。
       - `paper-executor` 執行前検査: 注文方向に応じた実際の参照板価格（買い: 最良 Ask、売り: 最良 Bid）を使用。

#### Reduce-only 特例（安全設計）
`reduce_only=true` かつ「既存ポジション全量を決済する注文」については、端数（dust）が残ってポジション解消が不能になる事態を防ぐため、`qty_step` や `min_notional`、`min_qty` の制約を免除し、ポジション解消を最優先します。

#### 実装後に確定した障害時挙動

- 新規注文のAPI受付でmetadata自体を取得できない場合は503。Market価格だけを取得できない場合はnotional検査をExecutorへ委任。
- 受付済み注文をExecutorが処理する際、metadataまたは注文板の例外・空応答・鮮度違反は一時障害として2秒から最大60秒まで再試行し、部分約定状態を維持。
- 取得済みmetadataの必須制約不足・不正・inactive・数量違反、および設定から削除された取引所・symbolだけを終端Reject。
- 注文・約定履歴は現行設定や外部metadataへ依存せずDBローカルで検索し、未設定取引所・未知symbolに該当がなければ200の空ページを返す。

---

### TC-D8: `request_id` 直接照合 API (`GET /api/v1/orders/by-request-id/{request_id}`)

#### 概要
`alpha_runtime` が発注後の状態ポーリングを最小のネットワーク負荷で行えるよう、既存の `orders.request_id` ユニークインデックスを活用した直接照合エンドポイントを追加します。

#### Endpoint 仕様
* **Path**: `GET /api/v1/orders/by-request-id/{request_id}`
* **Response Model**: `OrderView`
* **エラー**: 未存在時は `404 Not Found`。
* **新規受付制約**: `request_id` は `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`。既存unsafe IDのmigrationや`{request_id:path}` routeは追加しない。

---

### TC-D9: Paper 環境属性の明示的付与

#### 概要
誤って実取引所宛てのモジュールと混同されるリスクを排除するため、ヘルスチェックレスポンスに Paper 取引環境であることを明示します。

* `/health` および `/ready` レスポンスに `"environment": "paper"`, `"simulation": true` を追加。

---

## 4. データベースマイグレーション

PostgreSQL の変更は、`orders` テーブルへの監査タグ追加のみに限定されます。

### マイグレーション: `docker/db-migrate/copy/migrations/004_order_strategy_id.sql`
```sql
-- 注文テーブルへの strategy_id 追加（監査・追跡用）
ALTER TABLE orders ADD COLUMN IF NOT EXISTS strategy_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS ix_orders_strategy_id ON orders(strategy_id);
```

> [!NOTE]
> 外部市場データ（`instruments`）や仮想口座（`accounts`）のテーブル新設は見送られ、Trade Container のステートレス＆軽量原則が維持されます。

---

## 5. 実装ロードマップと受入ゲート (Acceptance Criteria)

> [!NOTE]
> 下記は当初のロードマップとGate条件です。2026-09-11時点でDocker自動テストは75件成功（警告1件）しました。外部取引所接続を含む実機受入試験は、実行環境に必要な秘密情報がないため未実施です。

```text
Phase 1: アダプター拡張 & 設定更新 (TC-D1, TC-D2)
   ↓
Phase 2: メタデータ・価格・残高 API の新設 (TC-D3, TC-D4, TC-D5)
   ↓
Phase 3: 監査タグ & 数量バリデーション & 照合API (TC-D6, TC-D7, TC-D8, TC-D9)
   ↓
Phase 4: Gate 再評価（crypto_alpha_platform 受入試験）
```

### Phase 4 受入完了条件（Gate 通過条件）
1. `GET /api/v1/exchanges` に `bybit` が含まれ、USDT無期限契約シンボルが取得できること。
2. `GET /api/v1/balance` で口座残高・余力が正常応答すること。
3. `GET /api/v1/prices` で Universe 銘柄のリアルタイム参照価格が取得できること。
4. `GET /api/v1/instruments` で `qty_step`, `min_qty`, `min_notional` が取得でき、数量検査が機能すること。
5. `POST /api/v1/orders` で Bybit 宛の成行注文（Market）、`reduce_only` 注文が 202 受理され、`paper-executor` でシミュレーション約定すること。
6. 全決済時に Reduce-only 特例が機能し、端数が安全に解消できること。
7. `GET /api/v1/orders/by-request-id/{request_id}` で即時照合できること。
8. 上記全項目が PASS した場合に限り、`crypto_alpha_platform` 側のサーキットブレーカーを解除し、Stage 5 / Stage 6 の実装に着手する。
