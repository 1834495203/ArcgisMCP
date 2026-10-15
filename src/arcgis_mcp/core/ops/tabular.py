"""Single-file CSV exports, with native typed GDB tables retained as an alternative."""
import contextlib
import csv
import shutil
import tempfile
from pathlib import Path

from .common import jsonable, path_exists


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
    try:
        arcpy.management.CreateFileGDB(str(directory), 'export.gdb')
        table = str(Path(gdb) / 'result')
        yield table
        fields = [f.name for f in arcpy.ListFields(table) if f.type not in ('Geometry', 'Blob', 'Raster')]
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        with open(output, 'w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            with arcpy.da.SearchCursor(table, fields) as cursor:
                for row in cursor:
                    writer.writerow([jsonable(v) for v in row])
    finally:
        with contextlib.suppress(Exception):
            if path_exists(gdb):
                arcpy.management.Delete(gdb)
        shutil.rmtree(str(directory), ignore_errors=True)
