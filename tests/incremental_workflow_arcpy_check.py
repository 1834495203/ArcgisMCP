"""Real draft -> final -> draft -> final -> delivery -> export/replay integration."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from arcgis_mcp.core.config import load_settings
from arcgis_mcp.core.history import History
from arcgis_mcp.core.project import ProjectRegistry
from arcgis_mcp.core.tool import Services
from arcgis_mcp.core.worker import ArcPyWorker
from arcgis_mcp.tools.workflow import (RunWorkflowStep, RunWorkflow, WorkflowSteps, ExportWorkflow,
    SaveWorkflowNode, RunSavedWorkflow, WorkflowNodes, ListWorkflows)


def main():
    with tempfile.TemporaryDirectory(prefix='.safety-incremental-', dir=ROOT) as folder:
        root = Path(folder)
        settings = load_settings()
        settings.forced_project = root
        settings.queue_timeout = 180
        worker = ArcPyWorker(settings)
        services = Services(settings, ProjectRegistry(settings), worker)
        ctx = SimpleNamespace(headers={})
        history = History(root)
        tool = RunWorkflowStep(services)
        checks = []
        def check(label, passed):
            assert passed, label
            checks.append(label)
            print('PASS', label, flush=True)
        def done(result, state='succeeded'):
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                job = history.get(result['job_id'])
                if job['state'] not in ('queued', 'starting', 'running'):
                    assert job['state'] == state, job
                    return job
                time.sleep(0.1)
            raise AssertionError('timeout')
        def run(step_id, body, inputs, mode='draft', outputs=None, edits=None, deliverables=None, state='succeeded'):
            script = root / (step_id + '.py')
            script.write_text(body, encoding='utf-8')
            result = json.loads(tool.run_workflow_step(ctx, 'soil', step_id, str(script),
                '步骤理由 ' + step_id, inputs, outputs if outputs is not None else {'result': '要素成果'},
                mode=mode, description='步骤说明 ' + step_id, parameters={'where': 'VALUE = 1'},
                input_descriptions={alias: '输入要素 ' + alias for alias in inputs},
                edit_inputs=edits, deliverables=deliverables, confirm=True))
            return done(result, state)
        def values(path):
            return [r['VALUE'] for r in worker.call('read', workspace=str(Path(path).parent),
                dataset=Path(path).name, fields=['VALUE'], limit=10)['records']]
        try:
            seed = root / 'seed.py'
            seed.write_text("import arcpy,sys\nr=sys.argv[1]\n"
                "arcpy.management.CreateFileGDB(r,'source.gdb')\n"
                "arcpy.management.CreateFeatureclass(r+'/source.gdb','A','POINT',spatial_reference=4326)\n"
                "arcpy.management.AddField(r+'/source.gdb/A','VALUE','LONG')\n"
                "with arcpy.da.InsertCursor(r+'/source.gdb/A',['SHAPE@XY','VALUE']) as c:\n"
                " c.insertRow([(110,30),1])\n c.insertRow([(111,31),2])\n"
                "import numpy as np\n"
                "arcpy.NumPyArrayToRaster(np.array([[1,2],[2,1]], dtype=np.int16), arcpy.Point(0,0),1,1).save(r+'/source.gdb/R')\n",
                encoding='utf-8')
            subprocess.run([settings.arcpy_python, str(seed), str(root)], check=True)
            original = str(root / 'source.gdb/A')
            select = ('arcpy.analysis.Select(inputs["features"], outputs["result"], parameters["where"])\n'
                      'step.check("caller count", int(arcpy.management.GetCount(outputs["result"])[0]) == 1, {"expected": 1})\n')
            draft = run('experiment', select, {'features': original})
            draft_path = draft['result']['outputs'][0]['path']
            check('draft outputs live only in temporary GDB', Path(draft_path).parent.name == 'temporary.gdb')
            failed = run('bad_dependency', select, {'features': draft_path}, 'final', state='failed')
            check('formal step refuses temporary dependency', '临时数据' in failed['error'])
            formal = run('select', select, {'features': original}, 'final')
            formal_path = formal['result']['outputs'][0]['path']
            check('formal output lives in process GDB', Path(formal_path).parent.name == 'process.gdb')
            analysis = run('inspect', 'step.check("count", int(arcpy.management.GetCount(inputs["features"])[0]) == 1)\n',
                {'features': formal_path}, outputs={})
            check('analysis-only draft records validation without a formal output',
                analysis['result']['outputs'] == [] and analysis['result']['validations'][0]['passed'])
            calculated = run('calculate', 'arcpy.management.CalculateField(outputs["result"], "VALUE", "7", "PYTHON3")\n',
                {'features': formal_path}, 'final', edits={'result': 'features'})
            changed = calculated['result']['outputs'][0]['path']
            check('edit copy retains prior formal data', values(formal_path) == [1] and values(changed) == [7])
            delivery = run('deliver', 'arcpy.management.CopyFeatures(inputs["features"], outputs["result"])\n',
                {'features': changed}, 'final', deliverables=['result'])
            delivered = delivery['result']['outputs'][0]['path']
            check('delivery data is separated from process and experiments',
                Path(delivered).parent.name == 'delivery.gdb' and values(delivered) == [7])
            listed = json.loads(WorkflowSteps(services).workflow_steps(ctx, 'soil', include_drafts=True, detail=True))
            check('all step bodies reasons bindings and validations persist', len(listed['steps']) == 5
                and all(s.get('reason') and s.get('description') and s.get('code') for s in listed['steps'])
                and any(s['validations'] for s in listed['steps']))
            exported = json.loads(ExportWorkflow(services).export_workflow(ctx, 'soil'))['files']
            exported_code = Path(exported['script']).read_text(encoding='utf-8')
            check('export excludes experiments and keeps readable final step code',
                'experiment' not in exported_code and 'inspect' not in exported_code
                and 'arcpy.management.CalculateField' in exported_code and '步骤理由 calculate' in exported_code)
            replay = json.loads(RunWorkflow(services).run_workflow(ctx, exported['script'], '重跑正式流程',
                json.loads(Path(exported['inputs']).read_text(encoding='utf-8')),
                json.loads(Path(exported['parameters']).read_text(encoding='utf-8')), confirm=True))
            rerun = done(replay)['result']['outputs'][0]['path']
            check('exported workflow reruns from original inputs with correct content', values(rerun) == [7])
            check('original and previous delivery remain unchanged', values(original) == [1, 2] and values(delivered) == [7])
            databases = {p.name for p in root.glob('*.gdb')}
            check('only source and three role GDBs were created: ' + str(sorted(databases)),
                databases == {'source.gdb', 'process.gdb', 'temporary.gdb', 'delivery.gdb'})
            second_export = json.loads(ExportWorkflow(services).export_workflow(ctx, 'soil'))
            check('repeated export retains prior script', second_export['files']['script'] != exported['script']
                and Path(exported['script']).exists())
            save = SaveWorkflowNode(services)
            def node(name, code, sources, edits=None, params=None, delivery=None):
                save.save_workflow_node(ctx, 'graph', name, code, '节点说明 ' + name, '节点理由 ' + name,
                    sources, {k: '节点输入 ' + k for k in sources}, {'result': '节点成果'},
                    parameters=params, edit_inputs=edits, deliverables=delivery)
            node('select', 'arcpy.analysis.Select(inputs["features"], outputs["result"], "VALUE = 1")',
                 {'features': original})
            node('calculate', 'arcpy.management.CalculateField(outputs["result"], "VALUE", str(parameters["value"]), "PYTHON3")',
                 {'features': '@select.result'}, edits={'result': 'features'}, params={'value': 8}, delivery=['result'])
            graph_nodes = json.loads(WorkflowNodes(services).workflow_nodes(ctx, 'graph'))
            check('saved graph queries references and validates topological order',
                graph_nodes['graph']['execution_order'] == ['select', 'calculate']
                and json.loads(ListWorkflows(services).list_workflows(ctx))['workflows'][0]['workflow_id'] == 'graph')
            saved = RunSavedWorkflow(services)
            first_graph = done(json.loads(saved.run_saved_workflow(ctx, 'graph', '直接只读引用原始输入',
                confirm=True, input_mode='read_only')))
            first_output = first_graph['result']['outputs'][0]['path']
            check('saved graph executes and retains original and first node data',
                values(first_output) == [8] and values(original) == [1, 2]
                and first_graph['result']['input_bindings']['select__features']['snapshot'] is None)
            second_graph = done(json.loads(saved.run_saved_workflow(ctx, 'graph', '同图新参数重跑',
                parameters={'calculate': {'value': 9}}, confirm=True)))
            second_output = second_graph['result']['outputs'][0]['path']
            check('graph rerun retains earlier outputs and uses new parameters',
                values(first_output) == [8] and values(second_output) == [9])
            listed = json.loads(WorkflowSteps(services).workflow_steps(ctx, 'graph', detail=True))
            check('saved graph records every executed node with code and named outputs',
                len(listed['steps']) == 4 and all(s['code'] and s['actual_outputs'][0]['name'] == 'result'
                                               for s in listed['steps']))
            graph_export = json.loads(ExportWorkflow(services).export_workflow(ctx, 'graph'))['files']
            exported_params = json.loads(Path(graph_export['parameters']).read_text(encoding='utf-8'))
            check('export selects one graph run and captures effective parameter overrides',
                exported_params['calculate']['value'] == 9
                and len(json.loads(Path(graph_export['steps']).read_text(encoding='utf-8'))) == 2)
            replay = done(json.loads(RunWorkflow(services).run_workflow(ctx, graph_export['script'], '重跑图导出',
                json.loads(Path(graph_export['inputs']).read_text(encoding='utf-8')),
                exported_params, confirm=True)))
            check('exported graph reruns with new dependency bindings and same actual data',
                values(replay['result']['outputs'][0]['path']) == [9] and values(original) == [1, 2])
            save.save_workflow_node(ctx, 'raster_graph', 'copy',
                'arcpy.management.CopyRaster(inputs["raster"], outputs["result"])\n'
                'step.check("pixels", arcpy.RasterToNumPyArray(inputs["raster"]).tolist() == [[1,2],[2,1]])',
                '读取分类栅格并保存工作成果', '验证不受单要素快照类型限制',
                {'raster': str(root / 'source.gdb/R')}, {'raster': '原始栅格'}, {'result': '栅格副本'})
            raster_job = done(json.loads(saved.run_saved_workflow(ctx, 'raster_graph', '只读栅格输入',
                confirm=True, input_mode='read_only')))
            raster_output = raster_job['result']['outputs'][0]['path']
            check('read-only raster input and registered raster output publish successfully',
                worker.call('describe', path=raster_output)['data_type'] == 'RasterDataset'
                and raster_job['result']['validations'][0]['passed'])
        finally:
            worker.shutdown()
    print(json.dumps({'passed': len(checks)}))


if __name__ == '__main__':
    main()
