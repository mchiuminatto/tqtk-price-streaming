"""Seeds the Redis calibration store - the only writer of its keyspace.

`tables.py` holds every seeded value; `keyspace.py` turns them into keys and writes them. Run with
`python -m tools.calibration_seed`, pointed at Redis by `TQTK_REDIS_URL`.
"""
