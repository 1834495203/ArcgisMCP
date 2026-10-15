"""Single-file CSV exports and bounded cleanup of owned temporary workspaces."""
import contextlib
import csv
import gc
import json
import os
import shutil
import tempfile
from pathlib import Path

from .common import jsonable, path_exists

MARKER = '.mcp-scratch.json'


def _write_csv(table, output):
    # Keep ArcPy cursor/row references in a separate frame, released before workspace deletion.
    import arcpy
    fields = [f.name for f in arcpy.ListFields(table) if f.type not in ('Geometry', 'Blob', 'Raster')]
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    with open(output, 'w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(fields)
        with arcpy.da.SearchCursor(table, fields) as cursor:
            for row in cursor:
                writer.writerow([jsonable(v) for v in row])


def _remove_scratch(directory):
    """Only called for a validated, privately owned directory while holding its workspace lease."""
    import arcpy
    gdb = str(directory / 'export.gdb')
    gc.collect()
    with contextlib.suppress(Exception):
        arcpy.management.ClearWorkspaceCache(gdb)
    with contextlib.suppress(Exception):
        if path_exists(gdb):
            arcpy.management.Delete(gdb)
    try:
        # Delete the marker last so partial cleanup remains retryable.
        for child in directory.iterdir():
            if child.name == MARKER:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(str(child))
            else:
                child.unlink()
        (directory / MARKER).unlink(missing_ok=True)
        directory.rmdir()
        return None
    except OSError as exc:
        payload = {'kind': 'csv_scratch_v1', 'pid': os.getpid(), 'cleanup_error': str(exc)}
        with contextlib.suppress(OSError):
            (directory / MARKER).write_text(json.dumps(payload), encoding='utf-8')
        return {'directory': str(directory), 'error': str(exc)}


def cleanup_scratch(root, limit=30):
    """Retry only marked CSV workspaces. Never infer ownership from a directory name alone."""
    from . import safety
    base = Path(root) / '.arcgis-mcp' / 'scratch'
    result = {'removed': 0, 'pending': []}
    if not base.is_dir() or base.is_symlink():
        return result
    for marker in list(base.glob('mcpwork_csv_*/' + MARKER))[:limit]:
        directory = marker.parent
        if directory.is_symlink() or directory.resolve().parent != base.resolve():
            continue
        try:
            if json.loads(marker.read_text(encoding='utf-8')).get('kind') != 'csv_scratch_v1':
                continue
            with safety.WorkspaceLease(str(directory / 'export.gdb')):
                error = _remove_scratch(directory)
                if error:
                    result['pending'].append(error)
                else:
                    result['removed'] += 1
        except Exception as exc:
            result['pending'].append({'directory': str(directory), 'error': str(exc)})
    return result


def scratch_status(root):
    """Read-only maintenance inventory; unmarked legacy directories are never auto-deleted."""
    base = Path(root) / '.arcgis-mcp' / 'scratch'
    result = {'managed_workspaces': 0, 'unmarked_directories': 0, 'pending_cleanup': []}
    if not base.is_dir() or base.is_symlink():
        return result
    for directory in base.glob('mcpwork_csv_*'):
        if not directory.is_dir() or directory.is_symlink():
            continue
        try:
            item = json.loads((directory / MARKER).read_text(encoding='utf-8'))
            if item.get('kind') != 'csv_scratch_v1':
                result['unmarked_directories'] += 1
                continue
            result['managed_workspaces'] += 1
            if item.get('cleanup_error') and len(result['pending_cleanup']) < 20:
                result['pending_cleanup'].append({'directory': str(directory), 'error': item['cleanup_error']})
        except (OSError, ValueError):
            result['unmarked_directories'] += 1
    return result


@contextlib.contextmanager
def table_output(output, clean_csv=True):
    """Run ArcPy against a private GDB, then stream a UTF-8 CSV without ArcPy sidecars."""
    if not clean_csv or Path(output).suffix.lower() != '.csv':
        yield output
        return
    import arcpy
    from . import safety
    base = (Path(safety.CURRENT_ROOT) / '.arcgis-mcp' / 'scratch'
            if safety.CURRENT_ROOT else Path(output).parent)
    base.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='mcpwork_csv_', dir=str(base)))
    gdb = str(directory / 'export.gdb')
    with safety.WorkspaceLease(gdb):
        (directory / MARKER).write_text(json.dumps({'kind': 'csv_scratch_v1',
            'pid': os.getpid(), 'job_id': safety.CURRENT_JOB}), encoding='utf-8')
        try:
            arcpy.management.CreateFileGDB(str(directory), 'export.gdb')
            table = str(Path(gdb) / 'result')
            yield table
            _write_csv(table, output)
        finally:
            _remove_scratch(directory)
