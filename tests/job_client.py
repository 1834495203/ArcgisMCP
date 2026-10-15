"""Shared HTTP test helper: poll background results without resubmitting writes."""
import asyncio
import json
import time


async def completed(session, data, timeout=7200):
    if not data.get('pending'):
        return data
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await session.call_tool('job_result', {'job_id': data['job_id']})
        text = ''.join(getattr(c, 'text', '') for c in result.content)
        snapshot = json.loads(text)
        if snapshot['state'] == 'succeeded':
            result = snapshot['result']
            return {'ok': True, **result, 'out_features': result.get('output'), 'job_id': data['job_id']}
        if snapshot['state'] in ('failed', 'unknown'):
            return {'ok': False, 'error': snapshot['error'], 'job_id': data['job_id']}
        await asyncio.sleep(0.2)
    raise TimeoutError('任务仍未完成：' + data['job_id'])
