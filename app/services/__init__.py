"""Business logic.

Nothing in this package imports FastAPI, Starlette or any HTTP concept. That is
deliberate: these modules are the units that get extracted into their own
services when the monolith outgrows one process. `routers/` is the disposable
adapter around them.
"""
