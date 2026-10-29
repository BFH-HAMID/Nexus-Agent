"""Runnable zero-dependency MCP plugin template. Stdout is JSON-RPC ONLY."""
import json
import sys

SCHEMA = {'type': 'object', 'properties': {'text': {'type': 'string', 'maxLength': 4000}}, 'required': ['text'], 'additionalProperties': False}
for line in sys.stdin:
    request = json.loads(line)
    method = request.get('method')
    if 'id' not in request:
        continue
    if method == 'initialize':
        result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'word-count-template', 'version': '1'}}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'count', 'description': 'Count words locally', 'inputSchema': SCHEMA}]}
    elif method == 'tools/call' and request['params']['name'] == 'count':
        result = {'content': [{'type': 'text', 'text': str(len(request['params']['arguments']['text'].split()))}]}
    else:
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'Unsupported method'}}), flush=True)
        continue
    print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': result}), flush=True)
