"""Copy-on-write publication and OS workspace locks. No MCP dependencies."""
from __future__ import annotations

import contextlib
import configparser
import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

from .common import WorkerError, path_exists

PREFIX = 'mcpwork_'
CURRENT_ROOT = None  # Worker protocol is serial; scoped/reset for every request.
CURRENT_JOB = None
DEFER_LEASE_RELEASE = False
CURRENT_LEASES = {}


def internal(path):
    return any(p.lower().startswith((PREFIX, 'mcpfailed_', 'mcptrash_'))
               or p.lower() == '.arcgis-mcp' for p in Path(str(path)).parts)


def workspace(path):
    target = Path(os.path.realpath(os.path.abspath(str(path))))
    for parent in (target, *target.parents):
        if parent.suffix.lower() in ('.gdb', '.mdb'):
            return str(parent)
    return str(target.parent)


class WorkspaceLease:
    """Fail fast. The worker holds the OS handle through copy, execute and publish."""
    def __init__(self, path):
        key = os.path.normcase(workspace(path))
        folder = Path(os.environ.get('ARCGIS_MCP_LOCK_DIR') or
                      str(Path(tempfile.gettempdir()) / 'arcgis-mcp-locks'))
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / (hashlib.sha256(key.encode('utf-8')).hexdigest() + '.lock')
        self.key = key
        self.handle = None
        self.borrowed = False

    def __enter__(self):
        self.borrowed = False
        if DEFER_LEASE_RELEASE and self.key in CURRENT_LEASES:
            self.borrowed = True
            return CURRENT_LEASES[self.key]
        self.handle = open(self.path, 'a+b')
        try:
            self.handle.seek(0, 2)
            if self.handle.tell() == 0:
                self.handle.write(b'\0')
                self.handle.flush()
            self.handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            raise WorkerError('WorkspaceBusy：工作区正在被其他 MCP 实例写入，请稍后重试：' + self.key) from exc
        if DEFER_LEASE_RELEASE:
            CURRENT_LEASES[self.key] = self
        return self

    def __exit__(self, *args):
        if self.borrowed or (DEFER_LEASE_RELEASE and self.key in CURRENT_LEASES):
            return
        self.release()

    def release(self):
        if self.handle:
            try:
                self.handle.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle, fcntl.LOCK_UN)
            finally:
                self.handle.close()
                self.handle = None


def release_leases():
    global DEFER_LEASE_RELEASE
    DEFER_LEASE_RELEASE = False
    for lease in list(CURRENT_LEASES.values()):
        with contextlib.suppress(Exception):
            lease.release()
    CURRENT_LEASES.clear()


def lock_error(exc):
    text = str(exc)
    if any(token in text.lower() for token in
           ('000464', '000496', '000601', 'schema lock', 'cannot acquire',
            'being used by another', 'exclusive lock', '无法获取锁', '无法获得独占', '被另一')):
        return WorkerError('WorkspaceBusy：数据被占用。请关闭 ArcGIS Pro 中使用该数据的图层、'
                           '属性表或编辑会话，或稍后重试。\n' + text)
    return exc


def sibling(path, prefix):
    p = Path(path)
    return str(p.with_name(prefix + uuid.uuid4().hex[:12] + p.suffix))


def version_path(path, operation='archive'):
    """Choose a readable retained name; caller must hold the workspace lease.

    Random names are reserved for internal staging. Existing versions are never overwritten.
    """
    p = Path(path)
    # Feature classes can contain dots; only ordinary file extensions are retained.
    suffix = p.suffix if not any(x.suffix.lower() in ('.gdb', '.mdb') for x in p.parents) else ''
    stem = p.name[:-len(suffix)] if suffix else p.name
    label = re.sub(r'[^a-zA-Z0-9_]', '_', str(operation)).strip('_')[:48] or 'archive'
    number = 1
    while True:
        tail = '_' + label + (f'_{number}' if number > 1 else '')
        candidate = str(p.with_name(stem[:120 - len(tail)] + tail + suffix))
        # An orphaned shapefile sidecar also reserves a basename; never replace it accidentally.
        companions = ([str(Path(candidate).with_suffix(ext)) for ext in
                       ('.dbf', '.shx', '.prj', '.cpg', '.sbn', '.sbx')] + [candidate + '.xml']
                      if suffix.lower() == '.shp' else [])
        if not path_exists(candidate) and not any(os.path.lexists(x) for x in [candidate] + companions):
            return candidate
        number += 1


def move_dataset(source, destination):
    import arcpy
    if path_exists(destination):
        raise WorkerError('发布路径已存在，拒绝覆盖：' + destination)
    suffix = Path(source).suffix.lower()
    if suffix in ('.json', '.geojson', '.xlsx', '.xls', '.csv', '.txt', '.png', '.pdf'):
        # ArcPy Rename is not applicable to arbitrary exported documents.
        os.rename(source, destination)
        for extra in ('.xml',):
            if os.path.exists(source + extra):
                os.rename(source + extra, destination + extra)
        if suffix == '.csv':
            schema = Path(source).parent / 'schema.ini'
            if schema.exists():
                parser = configparser.ConfigParser(interpolation=None)
                parser.optionxform = str
                encoding = 'utf-8-sig'
                try:
                    content = schema.read_text(encoding=encoding)
                except UnicodeDecodeError:
                    encoding = 'mbcs' if os.name == 'nt' else 'latin-1'
                    content = schema.read_text(encoding=encoding)
                parser.read_string(content)
                if parser.has_section(Path(source).name):
                    values = dict(parser.items(Path(source).name))
                    parser.remove_section(Path(source).name)
                    parser[Path(destination).name] = values
                    staged = schema.with_name('mcpwork_' + uuid.uuid4().hex + '.ini')
                    with open(staged, 'w', encoding=encoding) as stream:
                        parser.write(stream)
                    os.replace(str(staged), str(schema))
    elif os.path.isdir(source) and suffix not in ('.gdb', '.crf'):
        os.rename(source, destination)
    else:
        alias = getattr(arcpy.Describe(source), 'aliasName', None)
        if alias in (Path(source).name, Path(source).stem):
            alias = Path(destination).stem if Path(destination).suffix else Path(destination).name
        arcpy.management.Rename(source, destination)
        if alias and Path(destination).suffix.lower() != '.shp':
            arcpy.AlterAliasName(destination, alias)


class Publication:
    def __init__(self, requested, overwrite=False, root=None):
        if internal(requested):
            raise WorkerError('输出不能使用 MCP 内部暂存名字：' + requested)
        self.requested = requested
        if path_exists(requested) and not overwrite:
            raise WorkerError('输出已存在；换名或设置 overwrite=true（先保留旧版，再替换目标）：' + requested)
        self.final = requested
        self.overwrite = overwrite
        self.backup = None
        self.archives = []
        self.work = sibling(self.final, PREFIX)
        self.manifest = None
        if root:
            folder = Path(root) / '.arcgis-mcp' / 'transactions'
            folder.mkdir(parents=True, exist_ok=True)
            self.manifest = folder / (uuid.uuid4().hex + '.json')
        self.stage('prepared')

    def stage(self, state):
        if self.manifest:
            payload = {'pid': os.getpid(), 'state': state, 'work': self.work,
                       'output': self.final, 'requested_output': self.requested,
                       'job_id': CURRENT_JOB, 'backup': self.backup}
            temp = self.manifest.with_suffix('.tmp')
            with open(temp, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.manifest)

    def publish(self, actual=None):
        actual = actual or self.work
        if not path_exists(actual):
            raise WorkerError('工具没有生成有效输出：' + actual)
        try:
            if path_exists(self.final):
                if not self.overwrite:
                    raise WorkerError('发布目标已存在，拒绝覆盖：' + self.final)
                self.backup = version_path(self.final, 'overwrite')
                self.stage('backing_up')
                move_dataset(self.final, self.backup)
                self.stage('backed_up')
            self.stage('publishing')
            move_dataset(actual, self.final)
        except Exception:
            # A failed replacement must restore the old logical path, never delete the old data.
            if self.backup and path_exists(self.backup):
                try:
                    if path_exists(self.final):
                        move_dataset(self.final, sibling(self.final, 'mcpfailed_'))
                    move_dataset(self.backup, self.final)
                    self.stage('rolled_back')
                except Exception:
                    self.stage('recovery_required')
            raise
        if self.backup:
            self.archives.append({'original_path': self.final, 'history_path': self.backup})
        self.stage('published')
        return self.final

    def retire(self):
        """Remove the original name by archiving it, retaining the complete dataset."""
        if not path_exists(self.final):
            raise WorkerError('要删除的数据集不存在：' + self.final)
        self.backup = version_path(self.final, 'delete_dataset')
        self.stage('retiring')
        move_dataset(self.final, self.backup)
        self.archives = [{'original_path': self.final, 'history_path': self.backup}]
        self.stage('retired')
        return self.backup

    def fields(self):
        return {'target_replaced': bool(self.archives), 'archived_outputs': self.archives,
                'previous_output': self.backup}


def copy_for_edit(source, destination):
    """Copy (not CopyFeatures), fail closed for unverified dependent datasets."""
    import arcpy
    desc = arcpy.Describe(source)
    if str(desc.dataType).lower() not in ('featureclass', 'shapefile', 'table', 'dbasetable'):
        raise WorkerError('当前安全复制只支持独立要素类和表：' + source)
    if (getattr(desc, 'relationshipClassNames', []) or getattr(desc, 'hasAttachments', False)
            or getattr(desc, 'attributeRules', []) or getattr(desc, 'isVersioned', False)
            or getattr(desc, 'editorTrackingEnabled', False)):
        raise WorkerError('此数据含关系/附件/属性规则/版本或编辑追踪，尚未验证复制保真，拒绝修改；原件保留。')
    if Path(source).parent.suffix.lower() not in ('.gdb', '.mdb') and Path(source).suffix.lower() != '.shp':
        # Feature dataset members can be connected to topology/network objects.
        if '.gdb' in source.lower():
            raise WorkerError('要素数据集成员的关联约束尚未验证，拒绝复制编辑。请先导出独立要素类。')
    def schema(path):
        fields = [(f.name, f.type, f.length, f.precision, f.scale, f.isNullable,
                   f.aliasName, getattr(f, 'domain', ''), getattr(f, 'defaultValue', None))
                  for f in arcpy.ListFields(path)]
        subtypes = arcpy.da.ListSubtypes(path) if Path(source).suffix.lower() != '.shp' else {}
        for item in subtypes.values():
            if not item.get('SubtypeField'):
                item.pop('Name', None)  # ArcPy returns the dataset name when no subtype exists.
        # Domain objects do not compare by identity across calls.
        def normalize(value):
            if isinstance(value, dict):
                return {str(k): normalize(v) for k, v in value.items()}
            if isinstance(value, (list, tuple)):
                return [normalize(v) for v in value]
            if hasattr(value, 'domainType'):
                return (value.name, value.domainType, getattr(value, 'codedValues', None),
                        getattr(value, 'range', None))
            return value
        detail = arcpy.Describe(path)
        sr = getattr(detail, 'spatialReference', None)
        geometry = (getattr(detail, 'shapeType', None), getattr(detail, 'hasZ', None),
                    getattr(detail, 'hasM', None), sr.exportToString() if sr else None)
        indexes = sorted((tuple(f.name for f in index.fields), index.isUnique, index.isAscending)
                         for index in arcpy.ListIndexes(path))
        global_fields = [f.name for f in arcpy.ListFields(path) if f.type == 'GlobalID']
        global_ids = None
        if global_fields:
            with arcpy.da.SearchCursor(path, global_fields) as cursor:
                global_ids = sorted(tuple(str(v) for v in row) for row in cursor)
        return fields, normalize(subtypes), int(arcpy.management.GetCount(path)[0]), geometry, indexes, global_ids
    before = schema(source)
    with arcpy.EnvManager(preserveGlobalIds=True):
        arcpy.management.Copy(source, destination)
    if getattr(desc, 'aliasName', None) and Path(source).suffix.lower() != '.shp':
        arcpy.AlterAliasName(destination, desc.aliasName)
    after = schema(destination)
    if before != after:
        raise WorkerError('副本字段/域/子类型或记录数与原件不一致，已停止编辑；原件保留。\n'
                          + repr({'source_schema': before, 'copy_schema': after}))


def recover(root):
    """Quarantine only registered unfinished work. Never delete original/old/published data."""
    folder = Path(root) / '.arcgis-mcp' / 'transactions'
    recovered = []
    for manifest in folder.glob('*.json'):
        try:
            item = json.loads(manifest.read_text(encoding='utf-8'))
            if item['state'] not in ('prepared', 'backing_up', 'backed_up', 'publishing',
                                     'recovery_required', 'retiring', 'rolled_back'):
                continue
            work = item['work']
            if not Path(work).name.startswith(PREFIX):
                continue
            with WorkspaceLease(work):
                # Acquiring the workspace lock proves no cooperating writer is active.
                backup, output = item.get('backup'), item['output']
                if backup and path_exists(backup):
                    if path_exists(output):
                        failed = sibling(output, 'mcpfailed_')
                        move_dataset(output, failed)
                        item['unconfirmed_output'] = failed
                    move_dataset(backup, output)
                    item['restored_original'] = output
                elif item['state'] == 'publishing' and not path_exists(work) and path_exists(output):
                    failed = sibling(output, 'mcpfailed_')
                    move_dataset(output, failed)
                    item['unconfirmed_output'] = failed
                if path_exists(work):
                    failed = sibling(work, 'mcpfailed_')
                    move_dataset(work, failed)
                    item['quarantined'] = failed
                item['state'] = 'interrupted'
                manifest.write_text(json.dumps(item, ensure_ascii=False), encoding='utf-8')
                recovered.append(item)
        except Exception:
            continue  # Another live worker or inaccessible data: leave intact.
    return recovered
