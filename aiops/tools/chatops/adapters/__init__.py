"""Chatops adapters — pluggable sinks.

Each adapter implements ``ChatOpsAdapter.send()`` and is registered with the
``ChatOpsClient`` at process startup:

    >>> from aiops.tools.chatops import get_client
    >>> from aiops.tools.chatops.adapters.jsonfile import JsonFileChatOpsAdapter
    >>> get_client().register(JsonFileChatOpsAdapter(Path("demo/audit/chatops.jsonl")))

D2 lands the WebSocket adapter (UI live feed); D3 lands the JSON file
adapter (audit trail and vendor-neutrality proof).
"""
