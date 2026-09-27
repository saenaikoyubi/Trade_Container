# DB 運用手順

PostgreSQL のマイグレーション、バックアップ、隔離リストアの手順です。

Bybit の canonical symbol と履歴照合、および全決済の親子関係について、この文書をデータ不変条件の正本とします。API 契約は [API仕様](api.md)、執行規則は [取引・執行仕様](trading-engine.md) を参照してください。

## 1. スキーママイグレーション

通常起動時に自動実行されます。手動で実行する場合は以下を実行します。

```powershell
docker compose --env-file .env.local -f docker/compose.yaml run --rm db-migrate
```

- `docker/db-migrate/copy/migrations/` 配下の未適用 SQL をトランザクション内で順次適用します。

### マイグレーション履歴一覧

| バージョン | ファイル名 | 概要 |
|---|---|---|
| `001` | `001_initial.sql` | 初期スキーマ作成（`orders`, `fills`, `positions`, `daily_pnl`, `control_flags`, `service_heartbeats`） |
| `002` | `002_order_retry_schedule.sql` | 注文リトライ制御用カラム（`next_attempt_at`, `retry_count`）の追加 |
| `003` | `003_close_only.sql` | Close-only / 取引制御フラグ管理機能の拡張 |
| `004` | `004_order_strategy_id.sql` | 注文テーブルへの監査タグ（`strategy_id`）カラム追加およびインデックス（`ix_orders_strategy_id`）作成 |
| `005` | `005_close_requests.sql` | 共通冪等性キー、全決済親処理・対象建玉、子注文の親子参照と逐次執行用インデックス |
| `006` | `006_close_generation_mode.sql` | 全決済親処理の子注文生成方式（`eager` / `incremental`） |

> [!NOTE]
> 外部市場データ（`instruments`）や仮想口座（`accounts`）のテーブル新設は見送られます。全決済の監査と冪等性に必要な親処理は永続化します。

### 全決済の追加スキーマ

- `request_keys.request_id` は通常注文・全決済子注文・全決済親処理に共通する一意キーです。操作種別と対象レコードIDを保持し、API間の同一キー利用も衝突として検出します。`orders.request_id` の一意制約は維持します。
- `close_requests` は親の `request_id`、正規化済み入力（`exchange_id`、`symbol` の指定有無と値、`strategy_id`）、`generation_mode`、受付時刻、状態、待機・失敗理由を保持します。既存行とv1は`eager`、v2は`incremental`です。`symbol` の省略と明示指定は別の入力です。同一キー・同一入力・同一生成方式の再送は市場情報を再取得せず既存の親処理を返し、異なる入力や生成方式は409です。
- `close_request_positions` は受付時にロックした建玉ごとの対象数量、現在の残数量、進捗・失敗理由を保持します。`symbol` 省略時に後から生じた建玉は、このスナップショットへ追加しません。対象建玉に非終端の全決済親処理を同時に複数関連付けません。
- `orders.close_request_id` と建玉ごとの子順序は親子関係と逐次執行を示します。子の `request_id` は親の内部ID・建玉ID・連番から128文字以内で一意に生成し、利用者は文字列を解析せずAPI応答の値を使います。追加生成した子も同じ親に属します。
- 親の作成、対象建玉のスナップショット、既存未完了注文への取消要求、初期子注文の作成、冪等性キーの登録は1トランザクションです。v1は初期子全件、v2は建玉ごとの最初の子だけを作り、以後の子は前の子の終端処理と同じトランザクションで追加します。受付前検査が1銘柄でも失敗すれば、いずれも作成しません。初回に対象建玉がなければ、子注文0件の`completed`親処理とキーを記録します。
- 受付済みの子注文数量は変更しません。残数量の再計算では不要な子を取消し、新しい子を追加して監査経緯を残します。約定と建玉更新は、同じトランザクションで現在建玉、Reduce-only、親・子の取消、Kill Switchを再確認します。

### データ不変条件と互換性

- `orders.request_id`は全取引所を通じて一意です。通常注文と全決済親処理の再送判定に用いる入力とHTTP応答は[API仕様](api.md)を正本とします。共通キーは`request_keys`に登録し、DB制約でも重複を防ぎます。
- 新規のBybit `orders.symbol`、`fills.symbol`、`positions.symbol`は、設定一覧への登録有無にかかわらず`BTCUSDT`形式の大文字市場IDで保存します。CCXT unified symbolは入力aliasであり保存表記にしません。他取引所のcanonical規則はそれぞれの設定に従います。
- Bybitの既存`request_id`照合では、`BASE/USDT:USDT`形式のaliasをDBローカルでcanonical化してから保存済み入力と比較します。再送判定のために外部metadataを更新しません。
- DBには、設定から削除された取引所・symbol、上場廃止したBybit銘柄、過去の表記を持つ監査記録が残り得ます。これらを設定変更時に移行・削除せず、履歴APIは外部metadataを参照せず検索します。Bybit の履歴検索では大文字 `BASE/USDT:USDT` 形式（`BASE` は1文字以上の英数字）を `BASEUSDT` に変換し、入力表記そのものと変換後のcanonical symbolの両方を照合します。例: `BTC/USDT:USDT` → `BTCUSDT`。形式外の文字列を機械的に変換せず、現在の設定・alias cache・上場状態に依存しません。
- 保存済み建玉と現在建玉の`symbol`検索にも同じDBローカル変換を使用します。取引所指定を省略した検索では入力表記そのものを全取引所に照合し、厳密なBybit aliasだけBybit canonical symbolにも照合します。設定から外れた取引所の建玉行や数量ゼロの行は、DBから削除せず保存済み建玉照会に残します。
- `strategy_id`はNULL許容の監査タグであり、テナント境界やポジション分離キーではありません。trade-uiが生成する直接注文・全決済注文では`manual`を強制します。
- `retry_count`と`next_attempt_at`はExecutorの一時障害再試行に使用します。metadata・注文板・Mark Priceの不足や、実際に検査する必須制約の欠落・不正では注文状態を非終端へ戻し、部分約定数量を保持します。`rejection_reason`は直近の執行理由として非終端でも使用し、正常な再評価で一時理由をクリアします。終端の拒否・部分約定後取消・手動取消理由は監査用に保持します。
- `daily_pnl.trade_date`は約定の`executed_at`をUTCに変換した日付です。後日記録・訂正した約定も元の約定日に反映し、新規建てと決済の手数料を含む手数料控除後の確定損益を集計します。`GET /api/v1/pnl`と`GET /api/v1/balance`はこの台帳の全期間合計を使います。
- 新規`request_id`はURL-safeな形式に制限されますが、過去のunsafe IDをDB移行しません。slashを含むlegacy IDはパス形式の直接照合APIの対象外であるため、履歴APIを使用します。

## 2. バックアップ

```powershell
.\scripts\backup-and-verify.ps1

# Linux / macOS
sh ./scripts/backup-and-verify.sh
```

- 出力先: `docker/db-backup/volume/output/`
- 生成物: `trade_<UTC>_<ランダム値>.dump`, 同名の`.dump.sha256`と`.metadata`。同じ秒の複数回実行でも上書きしません。
- metadata v2は全publicテーブル名・行数、マイグレーション一覧、全カラム定義、制約の名称・種類・対象列・参照先・検証状態の署名を保持します。アーカイブSHA256とともに隔離リストアで照合します。CHECK制約のSQL文字列はPostgreSQLのダンプ・再解析で同等の別表記になるため、署名には含めません。
- スクリプトは生成した**その1件**をtmpfs上の隔離DBへ復元し、検証成功後に同名`.dump.verified`を作成します。マーカーにはアーカイブSHA256と検証時刻を記録します。マーカーのないアーカイブは未検証として扱います。旧v1 metadataはv2検証器の対象外で、既存の監査ファイルとして保持します。

## 3. 隔離リストア検証

稼働中の `postgres` コンテナに影響を与えず、tmpfs 上の検証用 DB（`trade_restore`）に復元して整合性（全publicテーブルの件数、制約構造、カラム定義）を検証します。通常は上記のバックアップ・検証一体のスクリプトを使用します。既存アーカイブを手動で確認する場合は、そのアーカイブ名を明示して以下を実行します。

```powershell
$env:BACKUP_FILE = "trade_<UTC>_<ランダム値>.dump"
$env:RESTORE_CONFIRM = "isolated"

# 復元コンテナ起動・検証実行
docker compose --env-file .env.local -f docker/compose.yaml --profile restore up -d postgres-restore
docker compose --env-file .env.local -f docker/compose.yaml --profile restore run --rm db-restore

# クリーンアップ
docker compose --env-file .env.local -f docker/compose.yaml --profile restore stop postgres-restore
docker compose --env-file .env.local -f docker/compose.yaml --profile restore rm -f postgres-restore

Remove-Item Env:BACKUP_FILE
Remove-Item Env:RESTORE_CONFIRM
```

手動検証は成功結果を確認するための手順です。`.verified`マーカーが必要な運用バックアップは、上記の一体スクリプトで作成してください。CIの`tools`テストでは、独立したPostgreSQL 17にマイグレーションを適用し、注文・親処理・共通冪等性キーを含む実データを2件のアーカイブに保存して、それぞれを隔離DBへ復元・照合します。
