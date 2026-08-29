"""HTTP adapters.

Thin by design: parse, delegate to a service, serialise. No business rule lives
here, which is what lets `services/` be lifted into its own process later.
"""
