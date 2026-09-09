# tqtk-common

Shared package for the tqtk price pipeline: `Tick`/`Bar` contract types, Redis stream and key
name builders, `seq`/`session_id` helpers, the `/health`, `/ready`, `/metrics` server,
12-factor config loading, and structured JSON logging.

This package is one implementation of the language-neutral data contract; it is not the
contract itself. `Tick` and `Bar` are frozen pydantic models conforming to
`contracts/wire/*.schema.json`, validated at construction — so build a changed record with
`Model.model_validate({**old.to_dict(), ...})`, not `model_copy`, which by design skips
validation. `to_dict`/`from_dict` is the typed record the schemas describe; `to_wire`/`from_wire`
is the flat string map a Redis stream entry carries.
