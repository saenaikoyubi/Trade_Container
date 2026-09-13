# Database secret

The actual `postgres_password` file is stored outside the repository at
`<TRADE_SECRETS_DIR>\local\database\postgres_password`. `.env.local` supplies
the external root through `TRADE_SECRETS_DIR`. Compose mounts only this file
read-only into services that need database access.
