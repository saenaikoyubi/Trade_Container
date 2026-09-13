# Trade Container ドキュメント準拠実装計画 (Gate Compliance Implementation Plan)

> [!IMPORTANT]
> **アーカイブ済み・実装完了** — 本文は2026-09-09時点のギャップを基準に作成した実装計画です。2026-09-11に実装と自動検証を完了しました。「現行実装」「未実装」などの記述は計画策定時点の状態であり、現在仕様ではありません。現在の正本は[API仕様](../api.md)、[取引・執行仕様](../trading-engine.md)、[システム設計](../architecture.md)、[DB運用手順](../database.md)です。

本ドキュメントは、`crypto_alpha_platform` の「Gate: Trade_Container 実機受入調査」において検出された機能ギャップ（[migration.md](migration.md)）および既存仕様書（`api.md`, `architecture.md`, `trading-engine.md`）に準拠させるための詳細実装計画です。

grilling セッションを通じて合意された設計判断（Round 1 〜 Round 3）を網羅し、エッジケースや障害耐性まで定義した最終版となっています。

## 完了記録

- **基準日**: 2026-09-09
- **実装完了日**: 2026-09-11
- **自動検証**: Dockerテストスイート75件成功、警告1件（Starletteのanyio非推奨警告）
- **追加回帰検証**: 未設定取引所・未知symbol・alias cache・外部通信なしの履歴検索、およびExecutorのmetadata空応答・例外・復旧・部分約定維持・終端Reject条件を含む
- **手動受入**: 外部取引所接続を含む実機受入試験は、実行環境に必要な秘密情報がないため未実施
- **実際のテスト配置**: `test_config.py`、`test_ccxt_market_data.py`、`test_market_features.py`、`test_order_controls.py`、`test_history_api.py`、`test_paper_executor.py`、`test_trade_ui.py`ほか既存テストへ統合

---

## 1. 決定事項サマリー (Design Decisions from Grilling)

| 項目 | 決定仕様 | 根拠・方針 |
| :--- | :--- | :--- |
| **シンボル永続化** | DBには設定上の canonical 表記（`BTCUSDT`）で保存 | 受付表記にかかわらず注文・ポジション・履歴を一本化。CCXT統一表記（`BTC/USDT:USDT`）への変換はアダプター呼び出し境界でのみ実施。 |
| **シンボル表記揺れ許容** | `BTC/USDT:USDT` ⇔ `BTCUSDT` を透過的正規化 | クライアントがどちらの表記でリクエストしてきてもエイリアスマップにより自動解決して受理。 |
| **設定構成** | `settings.json` は 3 取引所共存（`bybit`, `binance`, `dydx`） | 既存テストスイートの互換性を壊さず、Bybit USDT無期限設定および仮想口座設定を追加。 |
| **残高API障害時挙動** | 価格取得失敗時はフェイルファスト（HTTP 503） | 不正確な余力返却によるアルゴリズムの誤動作を防止し、健全なリトライを促す。 |
| **成行Notional検査** | キャッシュ仲値で検査、不可時は Executor 板評価へ委任 | 即時検知性を保ちつつ、通信スパイク時の不要な注文拒否を防止。 |
| **Reduce-only 特例** | 保有数量と注文数量の完全一致（`quantity == abs(position.quantity)`） | 端数（dust）解消時の制約免除条件を決定論的かつ安全に判定。 |
| **メタデータ初期化** | FastAPI lifespan で事前ロード＋障害時は `/ready` 503 とバックグラウンド再試行 | 初回リクエストのレイテンシを排除。起動時の一時不通でもコンテナクラッシュループを防ぐ。 |
| **手動UI監査タグ** | `trade-ui` からの発注は `strategy_id: "manual"` を自動付与 | 手動介入履歴の抽出・監査を容易化。 |
| **注文照合 API** | `GET /api/v1/orders/by-request-id/{request_id}`（パスパラメータのみ） | グローバルユニークインデックスを活用した最小負荷の $O(1)$ 照合。 |
| **刻み幅検査ロジック** | Decimalによる剰余判定 `(quantity - min_qty) % qty_step == 0` | `0.005` や `0.25` のようなstepも正しく検査し、浮動小数点を使用しない。 |

---

### 1.1 障害・互換性に関する確定事項

- 注文、全決済、現在ポジション、市場情報のsymbol入力は厳格にcanonical化する。注文・約定履歴はDBローカル検索とし、入力表記を常に検索したうえでロード済みalias cacheだけをbest-effortで併用する。履歴検索は未設定exchange・未知symbol・外部障害で失敗させず、該当なしなら200の空ページとする。aliasは完全一致・case-sensitiveで、前後空白は全APIで拒否する。
- metadataは1時間fresh、更新失敗時は最大24時間last-known-goodを使用する。24時間超または初回未取得は503とし、Bybitについては`/ready`も503へ戻す。価格cacheは1秒で、期限後のstale値は使用しない。
- `/ready`の必須条件はDBとGate対象Bybit metadataのみ。Binance・dYdX障害は対象リクエストに隔離する。再試行は1, 2, 4, 8, 16, 32, 60秒、以降60秒とする。
- `request_id`の新規受付は `^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$` に限定する。既存unsafe IDのmigrationやpath converterは追加しない。
- `/prices`の複数symbol指定はatomicで、1件でもmid priceを取得できなければ全体を503とする。
- Paper口座ではUSD・USDC・USDTのみ1:1換算し、その他の決済通貨を持つopen positionがあればbalanceを503とする。
- UI proxyは直接注文と全決済の双方へ`strategy_id: "manual"`を強制注入し、ブラウザpayloadによる上書きを許可しない。
- Executorのmetadata取得例外・空応答は一時障害として2, 4, 8, 16, 32, 60秒、以降60秒で取消まで再試行する。部分約定状態は維持し、取得済みmetadataの不完全・inactive・制約違反および設定から削除されたexchange/symbolだけを終端Rejectとする。

---

## 2. 差分分析サマリー (Gap Analysis)

| 改修ID | 改修項目 | ドキュメント仕様（`migration.md` 等） | 計画策定時の実装状況 | 主な対応ファイル |
| :--- | :--- | :--- | :--- | :--- |
| **TC-D1** | Bybit USDT無期限対応 (`CcxtAdapter`) | CCXT options 渡し、シンボル表記揺れ解決（エイリアスマップ）、メタデータ/価格オンデマンド取得、TTLキャッシュ | options 渡し未対応、シンボル解決未実装、メタデータ/価格取得メソッドなし | `ccxt_adapter.py`<br>`factory.py`<br>`base.py` |
| **TC-D2** | 取引所・口座設定拡張 | `settings.json` に `bybit`（options, symbols, fees）および `account`（仮想口座初期残高・レバレッジ）を追加 | `binance`/`dydx` のみ定義。`account` / `options` 定義なし。`config.py` パース未実装 | `settings.json`<br>`config.py` |
| **TC-D3** | 銘柄メタデータ照会 API | `GET /api/v1/instruments`。CCXT `load_markets()` を1時間TTLインメモリ保持して整形返却 | エンドポイント未実装。`InstrumentView` モデルなし | `main.py` (trade-api)<br>`ccxt_adapter.py` |
| **TC-D4** | リアルタイム価格照会 API | `GET /api/v1/prices`。未保有銘柄を含む Mark/Last/Mid 価格をオンデマンド（1秒TTLキャッシュ）取得返却 | エンドポイント未実装。保有銘柄の仲値照会のみ | `main.py` (trade-api)<br>`ccxt_adapter.py` |
| **TC-D5** | 口座残高・余力照会 API | `GET /api/v1/balance`。設定初期残高＋手数料控除済み確定損益＋未実現損益からオンデマンド決定論的算出。手数料累計は監査表示のみ | エンドポイント未実装。`BalanceView` モデルなし | `main.py` (trade-api)<br>`valuation.py` |
| **TC-D6** | 監査タグ `strategy_id` | `orders` テーブルに `strategy_id VARCHAR(64)` 追加。API受付・履歴フィルタリング対応 | DBマイグレーションなし、モデル・API受付・返却・フィルタ未実装 | `004_order_strategy_id.sql`<br>`models.py`<br>`main.py` |
| **TC-D7** | 銘柄別数量制約バリデーション | `min_qty`, `qty_step`, `min_notional` を同期（API: 422）＆執行前（Executor: Reject）二重検査。全決済時の Reduce-only 特例 | 静的大枠リミットのみ。銘柄別ステップ検査・特例処理なし | `risk.py`<br>`main.py` (trade-api)<br>`runner.py` |
| **TC-D8** | `request_id` 直接照合 API | `GET /api/v1/orders/by-request-id/{request_id}` による $O(1)$ 注文状態照会 | エンドポイント未実装（`order_id` 照会のみ） | `main.py` (trade-api) |
| **TC-D9** | Paper 環境属性の明示 | `/health` および `/ready` レスポンスに `environment: "paper"`, `simulation: true` を明示 | status のみ返却 | `main.py` (trade-api) |
| **DOCS** | ドキュメントの同期更新 | `docs/api.md`, `docs/trading-engine.md`, `docs/architecture.md` に新仕様を反映 | ドキュメント側は最新化済み。実装コードとの差分解消が必要 | `docs/api.md`<br>`docs/trading-engine.md`<br>`docs/architecture.md` |

---

## 3. 実装変更計画 (Proposed Implementation Steps)

### Phase 1: アダプター拡張 & 設定・DBマイグレーション (TC-D1, TC-D2, TC-D6)

#### 1. データベースマイグレーション
- **新規作成**: `docker/db-migrate/copy/migrations/004_order_strategy_id.sql`
  ```sql
  -- 注文テーブルへの strategy_id 追加（監査・追跡用）
  ALTER TABLE orders ADD COLUMN IF NOT EXISTS strategy_id VARCHAR(64);
  CREATE INDEX IF NOT EXISTS ix_orders_strategy_id ON orders(strategy_id);
  ```
- **修正**: `docker/share/copy/trade_common/models.py`
  - `Order` モデルに `strategy_id: Mapped[str | None] = mapped_column(String(64), index=True)` を追加。

#### 2. 取引所・口座設定の拡張
- **修正**: `docker/share/copy/trade_common/config.py`
  - `AccountSettings` dataclass を新設:
    ```python
    @dataclass(frozen=True)
    class AccountSettings:
        currency: str = "USDT"
        initial_balance: Decimal = Decimal("10000.0")
        default_leverage: Decimal = Decimal("10.0")
    ```
  - `ExchangeSettings` に `options: dict[str, Any] = field(default_factory=dict)` を追加。
  - `Settings` に `account: AccountSettings` を追加。
  - `Settings.from_file` で `options` および `account` をパース。
- **修正**: `docker/share/volume/config/settings.json`
  - `exchanges.bybit` を追加（既存 `binance`, `dydx` も維持）:
    ```json
    "bybit": {
      "adapter": "ccxt",
      "options": {
        "defaultType": "linear"
      },
      "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
      "fees": {
        "maker": "0.0002",
        "taker": "0.00055"
      }
    }
    ```
  - `account` を追加:
    ```json
    "account": {
      "currency": "USDT",
      "initial_balance": "10000.0",
      "default_leverage": "10.0"
    }
    ```
  - `risk` パラメータを Gate 基準値（`max_order_quantity: "10.0"`, `max_order_notional: "50000.0"`, `max_position_notional: "100000.0"`, `max_daily_loss: "5000.0"`, `max_orders_per_minute: 60`）に更新。

#### 3. CCXT アダプター拡張
- **修正**: `docker/share/copy/trade_common/exchange_adapters/base.py`
  - `ExchangeAdapter` プロトコルに `resolve_symbol`, `fetch_instruments`, `fetch_prices` を追加。
- **修正**: `docker/share/copy/trade_common/exchange_adapters/ccxt_adapter.py`
  - 初期化時に `config.options` を CCXT クライアントへ伝達（`client_params["options"] = config.options`）。
  - `_build_symbol_alias_map()` を実装し、`BTCUSDT` ⇔ `BTC/USDT:USDT` 間の相互解決を行う `resolve_symbol(raw_symbol)` を配備。
  - `fetch_order_book` で `resolve_symbol` を利用。
  - `fetch_instruments(symbol=None)`:
    - 1時間 TTL キャッシュ保持。
    - CCXT `markets` の `precision`（step/digits）, `limits`（min_qty, max_qty, min_notional）を正規化整形。
  - `fetch_prices(symbols=None)`:
    - 1秒 TTL キャッシュ保持。
    - CCXT `fetch_tickers` / `fetch_ticker` / `fetch_order_book` を利用し、Mark Price, Last Price, Bid, Ask, Mid を抽出・整形。
- **修正**: `docker/share/copy/trade_common/exchange_adapters/dydx_adapter.py`
  - プロトコルを満たすためのメソッド定義を追加。

---

### Phase 2: コア計算 & リスクバリデーション機構 (TC-D5, TC-D7)

#### 1. 口座残高・余力算出ロジック
- **修正**: `docker/share/copy/trade_common/valuation.py`
  - `calculate_account_balance(session, config, adapter_pool)` を実装:
    $$\text{Realized PnL} = \sum(\text{daily\_pnl.realized\_pnl})$$
    $$\text{Total Fee} = \sum(\text{fills.fee})$$
    $$\text{Unrealized PnL} = \sum(\text{position.quantity} \times (\text{current\_mid\_price} - \text{position.average\_entry\_price}))$$
    $$\text{Equity} = \text{initial\_balance} + \text{Realized PnL} + \text{Unrealized PnL}$$
  - `Realized PnL` は手数料控除済みであり、`Total Fee` は監査・表示用なので Equity から再減算しない。
    $$\text{Used Margin} = \sum(|\text{position.quantity}| \times \text{current\_mid\_price} / \text{default\_leverage})$$
    $$\text{Available Balance} = \max(0, \text{Equity} - \text{Used Margin})$$
  - 価格取得失敗時は `ValuationError` を送出し、呼び出し元で HTTP 503 として処理。

#### 2. 数量制約バリデーション & Reduce-only 特例
- **修正**: `docker/share/copy/trade_common/risk.py`
  - `validate_instrument_quantity(quantity, price, instrument, is_full_close)` を実装:
    - **Reduce-only 特例**: `reduce_only=True` かつ `quantity == abs(current_position.quantity)`（全決済）時は、端数解消のため `min_qty`, `qty_step`, `min_notional` 制約を免除。
    - 通常注文時:
      - 最小数量: `quantity >= instrument.min_qty`
      - 刻み幅: `(quantity - instrument.min_qty) % instrument.qty_step == 0`
      - 最小 Notional: `quantity * price >= instrument.min_notional`（成行注文で価格未取得時は Executor へ委任）
  - `evaluate_order`: 執行前の二重防壁として上記銘柄数量制約検査を組み込む。
- **修正**: `docker/share/copy/trade_common/runner.py`
  - `PaperExecutor._process`: `evaluate_order` 実行時に銘柄メタデータを渡すように連携。

---

### Phase 3: REST API 拡張 (`trade-api`) (TC-D3, TC-D4, TC-D5, TC-D6, TC-D7, TC-D8, TC-D9)

- **修正**: `docker/trade-api/copy/app/main.py`
  - **ライフサイクル（lifespan）拡張**:
    - 起動時に Bybit アダプターの `load_markets()` を非同期ウォームアップ。
    - 通信失敗時も起動継続し、準備完了まで `/ready` は 503。バックグラウンドで指数バックオフ再試行。
  - **スキーマ定義の追加・拡張**:
    - `InstrumentView`: `exchange_id`, `symbol`, `base_asset`, `quote_asset`, `settle_asset`, `contract_type`, `contract_size`, `qty_step`, `min_qty`, `max_qty`, `price_step`, `min_notional`, `status`
    - `PriceView`: `exchange_id`, `symbol`, `mark_price`, `last_price`, `bid_price`, `ask_price`, `mid_price`, `observed_at`
    - `BalanceView`: `currency`, `initial_balance`, `realized_pnl`, `unrealized_pnl`, `total_fee`, `equity`, `used_margin`, `available_balance`, `updated_at`
    - `OrderCreate`: `strategy_id: str | None = Field(default=None, max_length=64)` を追加。
    - `OrderView`: `strategy_id: str | None = None` を追加。
  - **新設エンドポイント**:
    - `GET /api/v1/instruments`: 銘柄メタデータ一覧取得（TC-D3）。
    - `GET /api/v1/prices`: リアルタイム参照価格取得（TC-D4）。
    - `GET /api/v1/balance`: 口座残高・余力照会（TC-D5、価格取得失敗時 503）。
    - `GET /api/v1/orders/by-request-id/{request_id}`: `request_id` 直接照合（TC-D8）。
  - **既存エンドポイント改修**:
    - `/health` および `/ready`: `"environment": "paper"`, `"simulation": true` をレスポンスに明示（TC-D9）。
    - `POST /api/v1/orders`:
      - シンボル表記揺れを透過解決（`BTC/USDT:USDT` ⇔ `BTCUSDT`）。
      - `strategy_id` の保存・返却。
      - 銘柄数量バリデーション（`min_qty`, `qty_step`, `min_notional`）同期検査（違反時 422）（TC-D7）。
      - Reduce-only 全決済特例の判定（`quantity == abs(position.quantity)`）。
      - 同一 `request_id` の重複判定処理（409/200）で `strategy_id` を突合対象に追加。
    - `GET /api/v1/history/orders`: `strategy_id` クエリパラメータによる絞り込みフィルタを追加（TC-D6）。
- **修正**: `docker/trade-ui/copy/app/main.py`
  - 手動発注時に `strategy_id: "manual"` を付与して `trade-api` へ中継。

---

## 4. 当初のテスト・検証計画 (Verification Plan)

### 自動テスト (Automated Tests)
`docker/test/copy/tests/` 配下に新規テストファイルを追加し、pytest で全項目を検証します。

1. **設定テスト (`test_config.py`)**:
   - `bybit` 取引所設定、`options`、`account` 設定が正しく読み込めることを検証。
2. **アダプターテスト (`test_ccxt_adapter.py`)**:
   - `options` の CCXT クライアントへの伝達、Bybit シンボルエイリアス解決、メタデータ整形、価格取得キャッシュを検証。
3. **メタデータ & 価格 API テスト (`test_instruments_and_prices_api.py`)**:
   - `GET /api/v1/instruments` および `GET /api/v1/prices` のレスポンススキーマ・キャッシュ動作を検証。
4. **残高 API テスト (`test_balance_api.py`)**:
   - `GET /api/v1/balance` における実現損益・未実現損益・手数料・レバレッジ別余力の計算整合性、価格取得失敗時 503 を検証。
5. **注文照合・監査タグテスト (`test_orders_extended.py`)**:
   - `POST /api/v1/orders` での `strategy_id` 永続化、`GET /api/v1/orders/by-request-id/{request_id}` の 200/404 照合、`GET /api/v1/history/orders` での絞り込みを検証。
6. **数量制約 & Reduce-only 特例テスト (`test_quantity_validation.py`)**:
   - `min_qty`, `qty_step` (Decimal剰余), `min_notional` 違反時の 422 応答 / Reject を検証。
   - `reduce_only=True` かつ全決済時の端数免除特例が正常にパスすることを検証。
7. **Paper 環境属性テスト (`test_paper_attributes.py`)**:
   - `/health` および `/ready` で `environment: "paper"`, `simulation: true` が返ることを検証。

### 手動・受入検証 (Manual / Gate Verification)
- Docker Desktop 起動後、`docker compose --env-file .env.local -f docker/compose.yaml run --rm test` を実行し、全テストパスを確認。
- Bybit USDT無期限（`BTCUSDT`）宛の成行発注を行い、202 受理、`paper-executor` でのシミュレーション約定、ポジション・残高反映の一連フローを確認。
