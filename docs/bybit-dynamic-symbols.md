# Bybit 動的銘柄対応

`settings.json` に事前登録していない Bybit の USDT Linear Perpetual をPaper取引で扱うための、機能概要と受入条件です。

## 仕様の正本

| 関心事 | 正本 |
|---|---|
| 対象市場の判定、API入力・応答・エラー、全決済親処理 | [API仕様](api.md) |
| 注文状態遷移、リスク検査、Mark PriceによるPaper約定・評価 | [取引・執行仕様](trading-engine.md) |
| canonical symbol、履歴のDBローカル照合、全決済の親子関係 | [DB運用手順](database.md) |
| 設定と操作例 | [Quickstart](quickstart.md) |

## 機能概要

- Bybitの市場metadataからUSDT建て・USDT決済のlinear swapと確認できる銘柄は、設定済み`symbols`に含まれなくても明示指定できます。他取引所の許可規則は変更しません。銘柄省略時の市場情報APIと`GET /api/v1/exchanges`は設定済み銘柄だけを返します。
- Bybitの大文字市場ID（例: `BTCUSDT`）をcanonical symbolとし、CCXT統一表記（例: `BTC/USDT:USDT`）も入力できます。新しい監査記録はcanonical symbolで保存し、設定や上場状態が変わっても履歴はDBだけで照合できます。
- 対象銘柄のMark Priceは照会ごとに新しく取得します。Bybit建玉の評価、および`inactive`/`unknown`市場での既存建玉のReduce-only成行決済に使用します。停止中の指値決済は扱いません。
- 建玉全決済は親の`request_id`で冪等化します。市場の成行数量上限を超える場合は子注文に分割し、部分約定の残数量も同じ親処理で再計画します。親の状態と全子注文は専用GETで確認できます。
- Bybit metadataは設定可能な5分TTL内だけ再利用し、期限切れの値へフォールバックしません。取得済みの制約が不正な通常注文は503と専用`reason_code`で示します。全決済で免除する最小制約は、値が欠けても決済を妨げません。

## 受入確認

1. 設定外の有効な対象銘柄を明示指定し、銘柄情報、Mark Price、取得時刻を取得できる。未知銘柄・対象外市場・取引不能・metadata不足をHTTPステータスと`reason_code`で区別できる。
2. 同じ銘柄でPaper注文・約定、建玉照会、Mark Price評価、縮小・全決済ができる。注文・約定・建玉・履歴のsymbolがcanonical表記で一致する。
3. `inactive`/`unknown`になった保有建玉を、Mark PriceがあればReduce-only成行で決済できる。指値決済は約定せず、受付済みの未完了指値は取り消される。
4. `max_market_qty`を超える建玉の全決済が上限内の複数子注文に分かれ、部分約定後の残数量も追跡できる。同じ親`request_id`の再送で新しい親を重複生成しない。異なる入力へのキー再利用は409になる。
5. 親処理のGET・取消、待機理由、銘柄ごとの残数量、子注文の監査履歴を確認できる。Kill Switch後に子注文が再開・再生成されない。
6. 上場廃止後もcanonical symbolと厳密なCCXT統一表記で履歴をDBローカル検索できる。銘柄省略時の市場情報APIは設定済み銘柄だけを返す。
