Launch `owner.py` and leave it serving. Then execute `contender.py`, which tries to use the same port.
Determine the identity of the server that actually owns the port by fetching its `/identity` endpoint.
Shut down the owner and reply with only the identity returned by HTTP.
