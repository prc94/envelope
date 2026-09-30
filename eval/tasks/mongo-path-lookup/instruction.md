A MongoDB server caches HTTP responses in a `responses` collection, with documents
`{path, body}` where `path` is a request path beginning with `/`. The `mongosh` client is
installed at `C:\Program Files\mongosh\mongosh.exe`. Connection details are in `mongo.env` in
this directory (`MONGO_HOST`, `MONGO_PORT`, `MONGO_DB`, `MONGO_USER`, `MONGO_PASSWORD`). Find the
document whose `path` is exactly `<<key_path>>` and reply with only its `body`.
