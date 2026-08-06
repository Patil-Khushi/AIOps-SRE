"""The ecommerce system-under-test.

Marks the folder as a package so ``demo.ecommerce.failure_injection`` is
importable from the demo UI server. The app itself (user-service/, order-service/,
…) is not Python-importable from here — each service is its own container image
with its own ``src/`` tree.
"""
