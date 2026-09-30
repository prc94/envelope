A MongoDB server holds an `orders` collection with documents `{order_id, customer, amount_cents}`.
Connection details are in `mongo.env` in this directory (`MONGO_HOST`, `MONGO_PORT`, `MONGO_DB`,
`MONGO_USER`, `MONGO_PASSWORD`). Sum `amount_cents` for customer `<<customer>>` and reply with
only the total in dollars, formatted like `1234.56`.
