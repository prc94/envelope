A Redis server tracks session state. Connection details are in `redis.env` in this directory
(`REDIS_HOST`, `REDIS_PORT`, `REDIS_USER`, `REDIS_PASSWORD`). Count how many keys matching
`<<key_prefix>>:session:*` have the value `active`, and reply with only that number.
