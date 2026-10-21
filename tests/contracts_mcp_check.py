"""Real stdio MCP schema/query check, without starting ArcPy or touching business data."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def main(profile='core'):
    with tempfile.TemporaryDirectory(prefix='.safety-mcp-', dir=ROOT) as folder:
        params = StdioServerParameters(command=sys.executable,
            args=['-m', 'arcgis_mcp', '--transport', 'stdio', '--project', folder], cwd=str(ROOT),
            env={'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8', 'ARCGIS_MCP_TOOL_PROFILE': profile})
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                catalog = await session.list_tools()
                names = {tool.name: tool for tool in catalog.tools}
                assert len(names) == (24 if profile == 'core' else 84), names.keys()
                if profile == 'core':
                    for hidden in ('run_arcpy_tool', 'run_unadapted_tool', 'intersect', 'calculate_fields', 'frequency'):
                        assert hidden not in names
                    rejected = await session.call_tool('run_arcpy_tool', {'tool': 'Delete_management', 'parameters': {}})
                    assert getattr(rejected, 'is_error', getattr(rejected, 'isError', False)), rejected
                expected_schemas = [('use_project', {'output_gdb', 'auto_create_gdb'}),
                    ('job_result', {'wait_seconds', 'detail'}), ('lineage', {'offset', 'limit', 'detail'}),
                    ('read_gdb_data', {'schema_detail'}),
                    ('job_logs', {'job_id', 'after', 'limit'}),
                    ('run_workflow', {'script_path', 'reason', 'inputs', 'parameters', 'output_gdb', 'timeout_seconds', 'confirm'}),
                    ('run_workflow_step', {'workflow_id', 'step_id', 'mode', 'outputs', 'edit_inputs', 'deliverables', 'input_descriptions'}),
                    ('configure_workflow_data', {'process_gdb', 'temporary_gdb', 'delivery_gdb', 'original_roots'}),
                    ('workflow_steps', {'workflow_id', 'include_drafts', 'detail'}),
                    ('export_workflow', {'workflow_id', 'source'}),
                    ('save_workflow_node', {'workflow_id', 'step_id', 'code', 'description', 'inputs', 'outputs'}),
                    ('workflow_nodes', {'workflow_id', 'detail'}),
                    ('list_workflows', {'offset', 'limit'}),
                    ('run_saved_workflow', {'workflow_id', 'reason', 'inputs', 'parameters', 'input_mode', 'confirm'})]
                if profile == 'legacy':
                    expected_schemas.extend([
                        ('zonal_statistics', {'clean_csv'}),
                        ('run_unadapted_tool', {'reason', 'input_parameters', 'output_parameter', 'edit_parameter'})])
                for name, expected in expected_schemas:
                    schema = getattr(names[name], 'inputSchema', None) or getattr(names[name], 'input_schema', {})
                    assert expected <= set(schema['properties']), (name, schema)
                    if name == 'run_unadapted_tool':
                        assert {'tool', 'reason', 'parameters', 'input_parameters'} <= set(schema['required'])
                    if name == 'run_workflow':
                        assert {'script_path', 'reason', 'inputs'} <= set(schema['required'])
                    if name == 'run_workflow_step':
                        assert {'description', 'input_descriptions', 'reason', 'inputs', 'outputs'} <= set(schema['required'])
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
                empty_plan = await call('workflow_steps', {'workflow_id': 'test'})
                assert empty_plan['total'] == 0 and empty_plan['layout'] is None
                assert (await call('list_workflows', {}))['total'] == 0
                await call('save_workflow_node', {'workflow_id': 'saved', 'step_id': 'first', 'code': 'pass',
                    'description': '节点说明', 'reason': '测试保存定义', 'inputs': {}, 'input_descriptions': {},
                    'outputs': {'result': '成果'}})
                nodes = await call('workflow_nodes', {'workflow_id': 'saved'})
                assert nodes['graph']['valid'] and nodes['total'] == 1 and 'code' not in nodes['nodes'][0]
                assert (await call('list_workflows', {}))['total'] == 1
                configured = await call('configure_workflow_data', {})
                assert Path(configured['layout']['temporary']).name == 'temporary.gdb'
                assert not (Path(folder) / 'temporary.gdb').exists()
                lineage = await call('lineage', {'path': './work.gdb/not_created', 'limit': 1})
                assert lineage['total_steps'] == 0 and lineage['next_offset'] is None
                explained = await call('explain', {'path': './work.gdb/not_created'})
                assert explained['explanation']
                missing = await call('job_result', {'job_id': 'not-created', 'wait_seconds': 0})
                assert missing['ok'] is False and missing['error_type'] == 'JobNotFound'
                missing_logs = await call('job_logs', {'job_id': 'not-created'})
                assert missing_logs['error_type'] == 'JobNotFound'
                assert not (Path(folder) / 'work.gdb').exists()
    print('PASS: ' + profile + ' MCP catalog, schemas, workspace selection and history queries')


if __name__ == '__main__':
    asyncio.run(main())
    asyncio.run(main('legacy'))
