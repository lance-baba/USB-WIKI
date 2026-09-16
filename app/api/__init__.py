"""业务 API 域模块包。

每个域模块只处理自己业务域的 HTTP 请求，统一通过 ``Handler`` 暴露的
response 原语（``_send_json`` / ``_read_json`` / ``ctx`` / SSE framing 等）
发响应；**不直接** ``send_response`` / ``send_header`` / ``wfile.write``，
也不自己做安全校验或密钥脱敏（这些集中在 ``app.server`` 的传输层）。

分发采用「薄分发」：``app/server.py`` 的 ``_route_get`` / ``_route_post``
依次询问各域模块的 ``handle_get`` / ``handle_post``，命中即返回。
"""
