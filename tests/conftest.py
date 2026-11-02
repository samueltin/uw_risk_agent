# test_server.py is a manual smoke script that calls asyncio.run() at import
# time, so collecting it would try to reach the MCP server. Run it directly:
#   python tests/test_server.py
collect_ignore = ["test_server.py"]
