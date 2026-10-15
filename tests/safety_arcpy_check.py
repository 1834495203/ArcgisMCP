"""Real ArcPy integration. Writes only a fresh temporary project beneath this repository."""
from pathlib import Path
import json
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from arcgis_mcp.core.config import load_settings
from arcgis_mcp.core.project import ProjectRegistry
from arcgis_mcp.core.tool import Services, Tool
from arcgis_mcp.core.worker import ArcPyWorker
from arcgis_mcp.core.history import History
from arcgis_mcp.tools.history import Explain, JobResult
from arcgis_mcp.tools.workspace import UseProject
from arcgis_mcp.tools.management import CopyFeatures
from arcgis_mcp.tools.gdb import ReadGdbData


class Probe(Tool):
    name = 'probe'
    def probe(self):
        pass


def main():
    with tempfile.TemporaryDirectory(prefix='.safety-arcpy-', dir=ROOT) as temporary:
        root = Path(temporary)
        settings = load_settings()
        settings.forced_project = root
        settings.job_wait_seconds = 0
        settings.queue_timeout = 180
        worker = ArcPyWorker(settings)
        services = Services(settings, ProjectRegistry(settings), worker)
        probe = Probe(services)
        ctx = SimpleNamespace(headers={})
        checks = []
        def check(label, condition):
            assert condition, label
            checks.append(label)
            print('PASS', label, flush=True)
        def completed(result):
            deadline = time.monotonic() + 150
            while result.get('pending') and time.monotonic() < deadline:
                snapshot = History(root).get(result['job_id'])
                if snapshot['state'] == 'succeeded':
                    return {**snapshot['result'], 'job_id': snapshot['job_id']}
                if snapshot['state'] in ('failed', 'unknown'):
                    raise AssertionError(json.dumps(snapshot, ensure_ascii=False))
                time.sleep(0.1)
            assert not result.get('pending'), 'job failed to finish'
            assert result.get('ok') is not False, result
            return result
        def write(op, **args):
            result = probe.arcpy(op, ctx=ctx, **args)
            check('write returns job_id', bool(result.get('job_id')))
            return completed(result)
        try:
            gdb = str(root / 'test.gdb')
            write('geoprocess', operation='create_gdb', inputs=[], output=gdb)
            original = write('geoprocess', operation='create_feature_class', inputs=[],
                output=gdb + '/original', params={'geometry_type': 'POINT', 'spatial_reference': 4326})['output']
            chosen = json.loads(UseProject(services).use_project(ctx, str(root),
                output_gdb='./test.gdb', auto_create_gdb=False))
            check('selected output workspace exposed', chosen['output_workspace'] == gdb and chosen['auto_create_gdb'] is False)
            try:
                write('geoprocess', operation='copy_features', inputs=[original], output=str(root/'unwanted.gdb/copy'))
            except AssertionError as exc:
                check('disabled automatic GDB creation fails explicitly', 'auto_create_gdb=false' in str(exc) and not (root/'unwanted.gdb').exists())
            else:
                raise AssertionError('unexpected GDB created')
            same_library = completed(json.loads(CopyFeatures(services).copy_features(ctx, original, 'same_workspace_copy')))
            check('bare output name reuses selected GDB', Path(same_library['output']) == Path(gdb) / 'same_workspace_copy')
            # Insert test features using a dedicated seed script, never arbitrary MCP code.
            seed = root / 'seed.py'
            seed.write_text("import arcpy,sys,os\np=sys.argv[1]\ngdb=os.path.dirname(p)\n"
                            "arcpy.management.CreateDomain(gdb,'CategoryDomain','category','LONG','CODED')\n"
                            "arcpy.management.AddCodedValueToDomain(gdb,'CategoryDomain',1,'one')\n"
                            "arcpy.management.AddCodedValueToDomain(gdb,'CategoryDomain',2,'two')\n"
                            "arcpy.management.AddField(p,'CATEGORY','LONG')\n"
                            "arcpy.management.AssignDomainToField(p,'CATEGORY','CategoryDomain')\n"
                            "arcpy.management.SetSubtypeField(p,'CATEGORY')\n"
                            "arcpy.management.AddSubtype(p,1,'one')\n"
                            "arcpy.management.AddSubtype(p,2,'two')\n"
                            "arcpy.management.SetDefaultSubtype(p,1)\n"
                            "arcpy.management.AddGlobalIDs(p)\n"
                            "with arcpy.da.InsertCursor(p, ['SHAPE@XY','CATEGORY']) as c:\n"
                            "    c.insertRow([(110,30),1])\n    c.insertRow([(111,31),2])\n"
                            "import numpy as np\n"
                            "r=arcpy.NumPyArrayToRaster(np.array([[0,1],[1,0]],dtype=np.int16), arcpy.Point(0,0),1,1)\n"
                            "r.save(gdb+'/classified')\n"
                            "arcpy.management.BuildRasterAttributeTable(gdb+'/classified','Overwrite')\n", encoding='utf-8')
            import subprocess
            subprocess.run([settings.arcpy_python, str(seed), original], check=True)
            added = write('edit', operation='add_field', target=original,
                          params={'field': 'VALUE', 'field_type': 'LONG'})['output']
            before = worker.call('read', workspace=gdb, dataset='original', limit=10)
            check('fixed edit uses tool suffix', Path(added).name == 'original_add_field')
            check('source schema retained', 'VALUE' not in [f['name'] for f in before['fields']])
            copied = worker.call('read', workspace=gdb, dataset=Path(added).name, limit=10)
            check('global IDs retained in copy', [r['GlobalID'] for r in before['records']] ==
                  [r['GlobalID'] for r in copied['records']])
            calculated = write('edit', operation='calculate_fields', target=added,
                params={'assignments': {'VALUE': '7'}, 'where': 'OBJECTID = 1'})['output']
            rows = worker.call('read', workspace=gdb, dataset=Path(calculated).name, fields=['VALUE'], limit=10)
            check('chained edit has readable name', Path(calculated).name == 'original_add_field_calculate_fields')
            branch = write('edit', operation='calculate_fields', target=added,
                params={'assignments': {'VALUE': '8'}, 'where': 'OBJECTID = 1'})['output']
            check('existing edit name adds numeric suffix', branch == calculated + '_2')
            check('where updates one row only', [r['VALUE'] for r in rows['records']] == [7, None])
            previous = worker.call('read', workspace=gdb, dataset=Path(added).name, fields=['VALUE'], limit=10)
            check('previous values retained', [r['VALUE'] for r in previous['records']] == [None, None])
            out = gdb + '/result'
            first_output = write('geoprocess', operation='copy_features', inputs=[calculated], output=out)
            repeated = write('geoprocess', operation='copy_features', inputs=[original], output=out, overwrite=True)
            check('overwrite replaces requested path', repeated['output'] == out and repeated['target_replaced'])
            check('old output retained', worker.call('describe', path=repeated['previous_output'])['record_count'] == 2)
            check('overwritten data has readable archive name', Path(repeated['previous_output']).name == 'result_overwrite')
            new_schema = worker.call('read', workspace=gdb, dataset='result', limit=0)
            check('requested name actually contains new data', 'VALUE' not in [f['name'] for f in new_schema['fields']])
            check('old job points to retained data', History(root).get(first_output['job_id'])['result']['output'] == repeated['previous_output'])
            check('editing response preserves input identity', first_output['inputs'] == [calculated])
            generic = write('run_arcpy_tool', tool='CalculateField_management', parameters={
                'in_table': calculated, 'field': 'VALUE', 'expression': '9', 'expression_type': 'PYTHON3'})
            check('generic edits copy first', generic['output'] != calculated)
            check('generic edit uses logical tool suffix', generic['output'] == calculated + '_calculate_fields')
            generic_copy = write('run_arcpy_tool', tool='CopyFeatures_management', parameters={
                'in_features': generic['output'], 'out_feature_class': gdb + '/generic'})
            check('generic producers audited', len(History(root).lineage(generic_copy['outputs'][0]['path'])['steps']) == 5)
            ranked = write('run_arcpy_tool', tool='Intersect_analysis', parameters={
                'in_features': [['./test.gdb/original', 1], ['./test.gdb/' + Path(calculated).name, 1]],
                'out_feature_class': './test.gdb/ranked_intersection'})
            check('ranked input value-table paths resolve against project', len(ranked['inputs']) == 2 and
                  worker.call('describe', path=ranked['outputs'][0]['path'])['record_count'] == 2)
            generic_create = write('run_arcpy_tool', tool='CreateFeatureclass_management', parameters={
                'out_path': gdb, 'out_name': 'generic_created', 'geometry_type': 'POINT', 'spatial_reference': 4326})
            check('generic split output safely published', generic_create['outputs'][0]['path'] == gdb + '/generic_created' or
                  Path(generic_create['outputs'][0]['path']).name == 'generic_created')
            removed = write('edit', operation='delete_features', target=calculated,
                            params={'where': 'VALUE=7', 'confirm': True})
            check('selected records deleted only from copy', removed['deleted_count'] == 1 and
                  worker.call('describe', path=calculated)['record_count'] == 2)
            check('edit returns original in_features', removed['in_features'] == calculated and removed['source_modified'] is False)
            generic_removed = write('run_arcpy_tool', tool='DeleteFeatures_management',
                                    parameters={'in_features': calculated}, confirm=True)
            check('generic DeleteFeatures has supported safe exit', generic_removed['deleted_count'] == 2 and
                  worker.call('describe', path=generic_removed['output'])['record_count'] == 0)
            check('fixed and generic edits share collision counter', generic_removed['output'] == removed['output'] + '_2')
            rat = worker.call('read', workspace=gdb, dataset='classified', fields=['Value', 'Count'], limit=10)
            check('raster attribute table exposes classes', rat['is_raster_attribute_table'] and
                  sorted((r['Value'], r['Count']) for r in rat['records']) == [(0, 2), (1, 2)])
            rat_filtered = worker.call('read', workspace=gdb, dataset='classified', where='Value=1', limit=10)
            check('raster table where works', rat_filtered['matched_count'] == 1)
            polygons = write('geoprocess', operation='raster_to_polygon', inputs=[gdb+'/classified'],
                output=gdb+'/classified_polygons', params={'raster_field':'Value','simplify':False,'create_multipart':False})
            check('raster classification field reported from real schema', polygons['output_value_field'].lower() == 'gridcode')
            check('raster conversion returns effective options only', polygons['effective_parameters']['simplify'] is False and 'validation_status' not in polygons)
            zonal_path = str(root/'zonal/zones.csv')
            write('geoprocess', operation='zonal_statistics', inputs=[polygons['output'],gdb+'/classified'],
                output=zonal_path, params={'zone_field':polygons['output_value_field'],'statistics_type':'ALL'})
            check('zonal statistics exports single CSV', [p.name for p in Path(zonal_path).parent.iterdir()] == ['zones.csv'])
            check_path = str(root/'geometry/check.csv')
            write('geoprocess', operation='check_geometry', inputs=[original], output=check_path)
            check('check geometry exports single CSV', [p.name for p in Path(check_path).parent.iterdir()] == ['check.csv'])
            compact = json.loads(ReadGdbData(services).read_gdb_data(ctx, gdb, dataset='original', limit=0))
            detailed = json.loads(ReadGdbData(services).read_gdb_data(ctx, gdb, dataset='original', limit=0, schema_detail=True))
            check('read schema defaults to name and type', all(set(f) <= {'name','type'} for f in compact['fields']))
            check('full field metadata remains available', any('length' in f for f in detailed['fields']))
            rat_export = write('run_arcpy_tool', tool='TableToTable_conversion', parameters={
                'in_rows': './test.gdb/classified', 'out_path': './rat_export', 'out_name': 'classes.dbf'})
            check('TableToTable relative workspace resolves', Path(rat_export['outputs'][0]['path']).exists())
            csv_output = str(root / 'clean' / 'frequency.csv')
            frequency = write('geoprocess', operation='frequency', inputs=[calculated], output=csv_output,
                              params={'frequency_field': ['CATEGORY']})
            check('CSV output has no schema or XML sidecars', [p.name for p in Path(csv_output).parent.iterdir()] == ['frequency.csv'])
            check('frequency explains grouping semantics', 'FREQUENCY' in frequency['frequency_semantics'])
            summary_csv = str(root / 'summary' / 'stats.csv')
            write('geoprocess', operation='summary_statistics', inputs=[calculated], output=summary_csv,
                  params={'statistics_fields': ['CATEGORY COUNT']})
            check('summary CSV is also a single file', [p.name for p in Path(summary_csv).parent.iterdir()] == ['stats.csv'])
            native = write('geoprocess', operation='frequency', inputs=[calculated], output=gdb + '/frequency',
                           params={'frequency_field': ['CATEGORY']})
            check('GDB table output remains supported', native['record_count'] == 2)
            check('CSV scratch directories are removed while worker remains alive', list((root/'.arcgis-mcp/scratch').glob('mcpwork_csv_*')) == [])
            history = History(root).lineage(repeated['previous_output'])
            check('history includes all processing steps', len(history['steps']) == 4)
            check('history has no internal names', 'mcpwork_' not in json.dumps(history))
            explained = json.loads(Explain(services).explain(ctx, out))
            check('explain lists retained results', Path(out).relative_to(root).as_posix() in explained['explanation'])
            items = worker.call('list_datasets', workspace=gdb)['items']
            check('listing hides internal work', all(not i['name'].startswith('mcpwork_') for i in items))
            shp = str(root / 'points.shp')
            write('geoprocess', operation='copy_features', inputs=[calculated], output=shp)
            shp_edit = write('edit', operation='add_xy_coordinates', target=shp)
            check('shapefile source retained', Path(shp).exists() and Path(shp_edit['output']).exists())
            check('shapefile family published', Path(shp_edit['output']).with_suffix('.dbf').exists())
            check('shapefile name preserves extension', Path(shp_edit['output']).name == 'points_add_xy_coordinates.shp')
            json_out = write('geoprocess', operation='features_to_json', inputs=[calculated],
                             output=str(root / 'export.json'), params={'geojson': True})
            check('actual geojson suffix published', json_out['output'].endswith('.geojson') and Path(json_out['output']).exists())
            deleted = write('edit', operation='delete_dataset', target=calculated, params={'confirm': True})
            check('delete archives data and removes original name', deleted['logical_deleted'] and
                  not deleted['original_path_exists'] and worker.call('describe', path=deleted['output'])['record_count'] == 2)
            check('deleted data has readable archive name', deleted['output'] == calculated + '_delete_dataset')
            try:
                worker.call('describe', path=calculated)
            except Exception:
                check('deleted original path cannot be opened', True)
            else:
                raise AssertionError('deleted source still readable')
            print(json.dumps({'passed': len(checks)}, ensure_ascii=False), flush=True)
        finally:
            if hasattr(worker, 'jobs'):
                worker.jobs.queue.join()
            worker.shutdown()


if __name__ == '__main__':
    main()
