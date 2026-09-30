A MongoDB server logs application events in an `events` collection (`ts`, `level`, `msg`). The
`mongosh` client is installed at `C:\Program Files\mongosh\mongosh.exe`. Connection details are in
`mongo.env` in this directory (`MONGO_HOST`, `MONGO_PORT`, `MONGO_DB`, `MONGO_USER`,
`MONGO_PASSWORD`). Count how many documents have `level` equal to `ERROR` and `ts` between
`<<start>>` and `<<end>>` inclusive, and reply with only that count.
