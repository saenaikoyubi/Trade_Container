# Trade Container

公開市場データを参照して約定シミュレーションを行う、Paper取引専用のDocker基盤です。注文ごとの `exchange_id` でCCXT対応取引所（Binance, Bybit等）またはdYdXへ振り分けます。実取引所への発注、取消、残高照会、認証情報の読込は一切行いません。

## 構成

- `trade-api`: 注文受付、取消・全決済要求、履歴・ポジション・損益照会、銘柄メタデータ・リアルタイム価格・口座残高照会、Close-only、Kill Switch
- `paper-executor`: 公開注文板取得、リスク検査（数量制約・銘柄メタデータ検査含む）、約定処理、ポジション・損益更新
- `trade-ui`: 手動注文、ポジション全決済、注文取消、Close-only操作、注文・約定履歴、ポジション・未実現損益照会（※Bot稼働時は緊急用）
- `postgres`: 注文（監査タグ含む）、約定、ポジション、日次損益、制御状態の永続化

## 起動

環境構築と操作方法は [Quickstart](docs/quickstart.md) を参照してください。

## ドキュメント

- [システム設計](docs/architecture.md)
- [取引・執行仕様](docs/trading-engine.md)
- [API仕様](docs/api.md)
- [Quickstart & 操作ガイド](docs/quickstart.md)
- [DB運用手順](docs/database.md)
- [移行・実装履歴](docs/archive/README.md)
