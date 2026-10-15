"""Real stdio MCP schema/query check, without starting ArcPy or touching business data."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main():
    with tempfile.TemporaryDirectory(prefix='.safety-mcp-', dir=ROOT) as folder:
        params = StdioServerParameters(command=sys.executable,
            args=['-m', 'arcgis_mcp', '--transport', 'stdio', '--project', folder], cwd=str(ROOT),
            env={'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                catalog = await session.list_tools()
                names = {tool.name: tool for tool in catalog.tools}
                for name, expected in [('use_project', {'output_gdb', 'auto_create_gdb'}),
                    ('job_result', {'wait_seconds', 'detail'}), ('lineage', {'offset', 'limit', 'detail'}),
                    ('read_gdb_data', {'schema_detail'}), ('zonal_statistics', {'clean_csv'})]:
                    schema = getattr(names[name], 'inputSchema', None) or getattr(names[name], 'input_schema', {})
                    assert expected <= set(schema['properties']), (name, schema)
                async def call(name, args):
                    response = await session.call_tool(name, args)
                    assert not getattr(response, 'is_error', getattr(response, 'isError', False)), response
                    return json.loads(''.join(c.text for c in response.content if hasattr(c, 'text')))
                selected = await call('use_project', {'folder': folder,
                    'output_gdb': './work.gdb', 'auto_create_gdb': False})
                assert selected['auto_create_gdb'] is False
                assert Path(selected['output_workspace']) == Path(folder) / 'work.gdb'
                listing = await call('list_datasources', {})
                assert listing['output_workspace'] == selected['output_workspace']
                lineage = await call('lineage', {'path': './work.gdb/not_created', 'limit': 1})
                assert lineage['total_steps'] == 0 and lineage['next_offset'] is None
                explained = await call('explain', {'path': './work.gdb/not_created'})
                assert explained['explanation']
                missing = await call('job_result', {'job_id': 'not-created', 'wait_seconds': 0})
                assert missing['ok'] is False and missing['error_type'] == 'JobNotFound'
                assert not (Path(folder) / 'work.gdb').exists()
    print('PASS: MCP schemas, workspace selection, compact history and missing-job result')


if __name__ == '__main__':
    asyncio.run(main())
