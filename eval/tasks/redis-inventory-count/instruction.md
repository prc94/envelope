A Redis server tracks warehouse inventory. The `redis-cli` client is installed at
`C:\Program Files\redis\redis-cli.exe`. Connection details are in `redis.env` in this directory
(`REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`). Count how many keys matching
`<<key_prefix>>:item:*` have the value `in-stock`, and reply with only that number.
