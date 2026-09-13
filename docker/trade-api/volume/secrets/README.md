# API token

The actual `api_token` file is stored outside the repository at
`<TRADE_SECRETS_DIR>\local\trade-api\api_token`. `.env.local` supplies the
external root through `TRADE_SECRETS_DIR`. Compose mounts the file read-only
into `trade-api` and `trade-ui`.
