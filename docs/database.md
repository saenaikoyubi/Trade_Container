# DB 運用手順

PostgreSQL のマイグレーション、バックアップ、隔離リストアの手順です。

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

> [!NOTE]
> 外部市場データ（`instruments`）や仮想口座（`accounts`）のテーブル新設は見送られ、Trade Container のステートレス＆軽量原則が維持されています。

### データ不変条件と互換性

- `orders.request_id`は全取引所を通じて一意です。同じキーの再送ではcanonical化後の注文内容と`strategy_id`まで比較し、同一なら既存注文を返し、相違があれば409とします。
- 新規の`orders.symbol`、`fills.symbol`、`positions.symbol`は、各取引所設定のsymbolをcanonical表記として保存します。取引所ネイティブIDやCCXT unified symbolはアダプター境界のaliasであり、新規データの保存表記にはしません。
- 既存DBには、現在の設定から削除された取引所・symbolや過去の表記を持つ監査記録が残り得ます。これらを設定変更時に移行・削除せず、履歴APIは外部metadataを参照せず入力表記で検索します。
- `strategy_id`はNULL許容の監査タグであり、テナント境界やポジション分離キーではありません。trade-uiが生成する直接注文・全決済注文では`manual`を強制します。
- `retry_count`と`next_attempt_at`はExecutorの一時障害再試行に使用します。metadata・注文板の一時障害では注文状態を非終端へ戻し、部分約定数量を保持します。
- 新規`request_id`はURL-safeな形式に制限されますが、過去のunsafe IDをDB移行しません。slashを含むlegacy IDはパス形式の直接照合APIの対象外であるため、履歴APIを使用します。

## 2. バックアップ

```powershell
docker compose --env-file .env.local -f docker/compose.yaml run --rm db-backup
```

- 出力先: `docker/db-backup/volume/output/`
- 生成物: `trade_<UTC>.dump`, `trade_<UTC>.dump.sha256`, `trade_<UTC>.metadata`

## 3. 隔離リストア検証

稼働中の `postgres` コンテナに影響を与えず、tmpfs 上の検証用 DB（`trade_restore`）に復元して整合性（テーブル件数、制約、カラム定義）を検証します。

```powershell
# 最新バックアップファイルを指定
$backup = Get-ChildItem docker/db-backup/volume/output/*.dump |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

$env:BACKUP_FILE = $backup.Name
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
