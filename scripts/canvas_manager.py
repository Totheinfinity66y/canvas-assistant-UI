"""Canvas library manager: read-only inventory, selected downloads, localhost UI.

No submission/upload endpoint is exposed. Requires only requests + Python 3.11.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urljoin, urlsplit, parse_qs
import webbrowser
import shutil

import requests
from canvas_settings import (SettingsError, LAYOUT, account_config, read_config, normalize_settings,
                             public_settings, initialization_plan, initialize_new, save_config)

SOURCE = Path(__file__).resolve().parents[1]
LIBRARY = Path.home() / 'Downloads' / 'Canvas'
CST = timezone(timedelta(hours=8))
MEDIA = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.mp3', '.wav', '.m4a', '.flv', '.wmv', '.aac', '.rmvb', '.m4v'}


class ManagerError(Exception):
    pass


def safe_name(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip('. ') or 'unnamed'
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', name, re.I):
        name = '_' + name
    return name[:180]


def local_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(CST)
    except (ValueError, TypeError):
        return None


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic_new(path, chunks, size=None, progress=lambda n: None):
    """Validate a unique sibling temp file, publish atomically without replacement."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.canvas-', suffix='.part', dir=path.parent)
    try:
        written = 0
        with os.fdopen(fd, 'wb') as stream:
            for chunk in chunks:
                if chunk:
                    stream.write(chunk)
                    written += len(chunk)
                    progress(written)
            stream.flush()
            os.fsync(stream.fileno())
        if not written or (size is not None and written != size):
            raise ManagerError('下载为空或文件大小不符')
        # Hard link creation is atomic and fails if the destination exists, on NTFS/POSIX.
        os.link(temporary, path)
        return written
    finally:
        Path(temporary).unlink(missing_ok=True)


def slide_folder(name):
    if re.search(r'大作业|项目|选题|竞赛|project|proposal', name, re.I):
        return 'project'
    if re.search(r'参考|教材|教程|安装|指南|manual|guide|往届|reference|示例', name, re.I):
        return 'reference'
    match = re.search(r'lecture[-_\s]*\d{4}[-_\s]*(\d{1,2})\b|(?:lecture|week|第)[-_\s]*(\d{1,2})(?:讲|周)?', name, re.I)
    if match:
        week = int(match.group(1) or match.group(2))
        if 1 <= week <= 30:
            return f'Week_{week:02d}'
    # Upload dates alone do not establish a reliable semester/week baseline.
    return ''


class AttachmentParser(HTMLParser):
    def __init__(self, base):
        super().__init__()
        self.base = base
        self.ids = set()

    def handle_starttag(self, tag, attrs):
        for key, value in attrs:
            if key not in ('href', 'src', 'data-api-endpoint') or not value:
                continue
            parsed = urlsplit(urljoin(self.base, value))
            if parsed.netloc != urlsplit(self.base).netloc:
                continue
            match = re.search(r'/(?:api/v1/)?(?:courses/\d+/)?files/(\d+)(?:/|$)', parsed.path)
            if match:
                self.ids.add(int(match.group(1)))


class CanvasClient:
    def __init__(self, config_path, profile=None):
        try:
            config = config_path if isinstance(config_path, dict) else json.loads(Path(config_path).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise ManagerError('无法读取 Canvas 配置文件，请检查 --config') from None
        profile = profile or config.get('canvas_default_profile')
        profiles = config.get('canvas_profiles') or {}
        if profile and profile not in profiles:
            raise ManagerError('指定的 Canvas profile 不存在')
        selected = profiles.get(profile, {}) if profile else {}
        self.base = (selected.get('base_url') or config.get('base_url') or 'https://oc.sjtu.edu.cn').rstrip('/')
        self.token = selected.get('token') or config.get('canvas_token')
        self.profile = profile or 'default'
        if not self.token or str(self.token).startswith('YOUR_'):
            raise ManagerError('Canvas token 尚未配置')
        if urlsplit(self.base).scheme != 'https':
            raise ManagerError('Canvas base_url 必须使用 HTTPS')

    def probe(self):
        executable = shutil.which('curl.exe' if os.name == 'nt' else 'curl')
        if not executable:
            raise ManagerError('找不到 curl；请按使用说明准备系统 curl 后重试')
        try:
            result = subprocess.run([executable, '-s', '-o', os.devnull, '-w', '%{http_code}', '--max-time', '8', self.base],
                                    capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            raise ManagerError('连通性探测失败；未扫描，不表示无作业') from None
        if result.returncode or result.stdout.strip() == '000':
            raise ManagerError('Canvas 当前不可达；已停止，未重试')
        self.get('/api/v1/users/self')

    def get(self, path, params=None):
        url = urljoin(self.base + '/', path)
        items, visited = [], set()
        try:
            while url:
                if urlsplit(url).netloc != urlsplit(self.base).netloc or urlsplit(url).scheme != 'https' or url in visited:
                    raise ManagerError('Canvas 分页地址异常')
                visited.add(url)
                with requests.get(url, params=params, headers={'Authorization': f'Bearer {self.token}'}, timeout=(8, 35)) as response:
                    if response.status_code in (401, 403):
                        raise ManagerError(f'Canvas 认证或权限不足（HTTP {response.status_code}）')
                    response.raise_for_status()
                    data = response.json()
                    if not isinstance(data, list):
                        return data
                    items.extend(data)
                    url = response.links.get('next', {}).get('url')
                    params = None
            return items
        except (requests.RequestException, ValueError):
            # requests exceptions include signed URLs; never expose their text.
            raise ManagerError('Canvas API 请求失败，请检查网络和权限') from None

    def download(self, file_id, path, size, progress, updated_at=None):
        metadata = self.get(f'/api/v1/files/{file_id}')
        if metadata.get('size') != size or (updated_at is not None and metadata.get('updated_at') != updated_at):
            raise ManagerError('远端文件已变化，请重新扫描')
        url = metadata.get('url') or ''
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.netloc:
            raise ManagerError('下载地址无效')
        headers = {'Authorization': f'Bearer {self.token}'} if parsed.netloc == urlsplit(self.base).netloc else {}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(8, 60)) as response:
                response.raise_for_status()
                if 'text/html' in response.headers.get('Content-Type', '').lower() and path.suffix.lower() not in ('.html', '.htm'):
                    raise ManagerError('服务器返回登录页，未保存为课件')
                return atomic_new(path, response.iter_content(128 * 1024), size, progress)
        except requests.RequestException:
            raise ManagerError('文件下载失败，请检查网络和权限') from None


class Manager:
    def __init__(self, client, root, course_map=None, max_mb=150, include_media=False, all_courses=False, layout=None, course_ids=None):
        self.client, self.root = client, Path(root).resolve()
        self.course_map = course_map or {}
        self.max_bytes = int(max_mb * 1024 * 1024)
        self.include_media, self.all_courses = include_media, all_courses
        self.layout = normalize_settings({'root':str(self.root), 'layout':layout or {}})['layout']
        self.course_ids = {str(i) for i in (course_ids or [])}
        self.items, self.errors, self.courses = [], [], []
        self.index_path = self.root / '.canvas-state' / 'index.json'
        self.index = {}
        if self.index_path.exists():
            try:
                self.index = json.loads(self.index_path.read_text(encoding='utf-8'))
                if not isinstance(self.index, dict):
                    raise ValueError()
            except (OSError, ValueError):
                raise ManagerError('下载索引无法读取，请检查 .canvas-state/index.json；未覆盖') from None

    def inside(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ManagerError('目标路径超出资料库')
        return path

    def identity(self, course_id, file_id):
        return f'{self.client.base}|{self.client.profile}|{course_id}|{file_id}'

    def course_dir(self, course):
        configured = self.course_map.get(str(course['id']))
        return self.inside(self.root / (configured or safe_name(course['name'])))

    def existing(self, item):
        indexed = self.index.get(item['identity'])
        if indexed and indexed.get('updated_at') == item.get('updated_at') and indexed.get('size') == item.get('size'):
            path = self.inside(self.root / indexed['path'])
            if path.is_file() and path.stat().st_size == indexed['size'] and digest(path) == indexed.get('sha256'):
                return path, '索引及 SHA-256 校验通过'
        if indexed:
            return None, ''  # A known changed/corrupt remote/local file must not fall back to name matching.
        if item.get('ambiguous'):
            return None, ''
        search_root = self.inside(self.root / item['search_root'])
        if search_root.is_dir():
            for path in search_root.rglob('*'):
                if path.name.casefold() == item['name'].casefold() and path.is_file():
                    self.inside(path)
                    if path.stat().st_size == item['size'] and item['size'] > 0:
                        return path, '历史文件名及大小匹配（未核验远端内容）'
        return None, ''

    def add_file(self, course, metadata, target, search_root, kind='file', assignment_id=None, ambiguous=False):
        name = safe_name(metadata.get('display_name') or metadata.get('filename') or f"file-{metadata['id']}")
        item = dict(key=f"{course['id']}:{kind}:{assignment_id or 0}:{metadata['id']}", kind=kind,
                    course_id=course['id'], course=course['name'], file_id=metadata['id'], name=name,
                    identity=self.identity(course['id'], metadata['id']), size=metadata.get('size') or 0,
                    updated_at=metadata.get('updated_at'), path=str(target / name),
                    search_root=str(search_root), status='pending', reason='', ambiguous=ambiguous)
        # Paths in inventory are relative; signed source URLs are deliberately excluded.
        item['path'] = str(self.inside(target / name).relative_to(self.root))
        item['search_root'] = str(self.inside(search_root).relative_to(self.root))
        existing, reason = self.existing(item)
        if existing:
            item.update(status='exists', path=str(existing.relative_to(self.root)), reason=reason)
        elif metadata.get('locked_for_user') or metadata.get('hidden_for_user'):
            item.update(status='skipped', reason='Canvas 文件不可访问')
        elif not self.include_media and Path(name).suffix.lower() in MEDIA:
            item.update(status='skipped', reason='音视频默认跳过；可用 --include-media')
        elif item['size'] > self.max_bytes:
            item.update(status='skipped', reason='超过大小限制；可调整 --max-mb')
        elif not item['size']:
            item.update(status='skipped', reason='空文件或源大小未知')
        self.items.append(item)

    def scan(self, progress=lambda **kw: None):
        self.items, self.errors = [], []
        self.client.probe()
        params = {'per_page': 100}
        if not self.all_courses:
            params['enrollment_state'] = 'active'
        self.courses = self.client.get('/api/v1/courses', params)
        if self.course_ids:
            self.courses = [c for c in self.courses if str(c['id']) in self.course_ids]
        directory_owners = {}
        for number, course in enumerate(self.courses):
            progress(stage='scan', current=number, total=len(self.courses), message=course.get('name', str(course['id'])))
            if not course.get('name'):
                self.errors.append(dict(course_id=course['id'], section='course', error='课程名称不可见，未扫描'))
                continue
            course_dir = self.course_dir(course)
            normalized = str(course_dir).casefold()
            if normalized in directory_owners:
                # Avoid merging courses whose names sanitize to the same directory.
                course_dir = self.inside(course_dir.with_name(course_dir.name + f"__{course['id']}"))
            directory_owners[normalized] = course['id']
            try:
                files = self.client.get(f"/api/v1/courses/{course['id']}/files", {'per_page': 100})
            except ManagerError as error:
                self.errors.append(dict(course_id=course['id'], section='files', error=str(error)))
                files = []
            by_id = {f['id']: f for f in files}
            name_counts = {}
            for f in files:
                name = safe_name(f.get('display_name') or f.get('filename') or f"file-{f['id']}").casefold()
                name_counts[name] = name_counts.get(name, 0) + 1
            for f in files:
                name = safe_name(f.get('display_name') or f.get('filename') or f"file-{f['id']}")
                category = slide_folder(name)
                if category.startswith('Week_'):
                    category = self.layout['week_prefix'] + category[5:]
                elif category:
                    category = self.layout[category]
                self.add_file(course, f, course_dir / self.layout['slides'] / category, course_dir / self.layout['slides'],
                              ambiguous=name_counts[name.casefold()] > 1)
            try:
                assignments = self.client.get(f"/api/v1/courses/{course['id']}/assignments", {'per_page': 100, 'include[]': 'submission'})
            except ManagerError as error:
                self.errors.append(dict(course_id=course['id'], section='assignments', error=str(error)))
                assignments = []
            assignment_targets = {}
            for assignment in assignments:
                due = local_time(assignment.get('due_at'))
                date = due.strftime('%Y-%m-%d') if due else 'no-date'
                directory = f"{date}_{safe_name(assignment['name'])}" if self.layout['date_assignments'] else safe_name(assignment['name'])
                target = course_dir / self.layout['homework'] / directory
                # Reuse an existing snapshot directory even if its due date has changed.
                for snapshot in (course_dir / self.layout['homework']).glob('*/assignment.json'):
                    try:
                        if json.loads(snapshot.read_text(encoding='utf-8')).get('id') == assignment['id']:
                            target = self.inside(snapshot.parent)
                            break
                    except (ValueError, OSError):
                        continue
                snapshot = target / 'assignment.json'
                occupied = False
                if snapshot.exists():
                    try:
                        occupied = json.loads(snapshot.read_text(encoding='utf-8')).get('id') != assignment['id']
                    except (OSError, ValueError):
                        occupied = True
                target_key = str(target).casefold()
                if occupied or (target_key in assignment_targets and assignment_targets[target_key] != assignment['id']):
                    target = target.with_name(target.name + f"__canvas-{assignment['id']}")
                assignment_targets[str(target).casefold()] = assignment['id']
                submission = assignment.get('submission') or {}
                workflow = submission.get('workflow_state') or 'unknown'
                submitted = workflow in ('submitted', 'pending_review') or bool(submission.get('submitted_at'))
                if submission.get('excused'):
                    state = '免交'
                elif submitted:
                    state = '已评分' if workflow == 'graded' else '已提交'
                elif workflow == 'unknown':
                    state = '状态未知'
                elif due and due < datetime.now(CST):
                    state = '逾期未交'
                else:
                    state = '待提交'
                snapshot_data = {k: assignment.get(k) for k in ('id', 'name', 'due_at', 'html_url', 'updated_at')}
                snapshot_data['submission'] = {k: submission.get(k) for k in ('workflow_state', 'submitted_at', 'late', 'missing', 'score', 'grade', 'excused')}
                contents = {'assignment.json': json.dumps(snapshot_data, ensure_ascii=False, indent=2),
                            'description.html': assignment.get('description') or '<p>无作业描述</p>',
                            'README.md': f"# {assignment['name']}\n\n截止（上海）：{due or '无'}\n\n提交状态：{state}\n\nCanvas：{assignment.get('html_url', '')}\n"}
                pending = [name for name in contents if not (target / name).exists()]
                self.items.append(dict(key=f"{course['id']}:assignment:{assignment['id']}", kind='assignment', course_id=course['id'],
                                       course=course['name'], name=assignment['name'], assignment_id=assignment['id'],
                                       path=str(self.inside(target).relative_to(self.root)), size=0, due_at=assignment.get('due_at'),
                                       canvas_url=f"{self.client.base}/courses/{course['id']}/assignments/{assignment['id']}",
                                       due_local=due.strftime('%Y-%m-%d %H:%M') if due else '无截止时间', submission=state,
                                       status='pending' if pending else 'exists', reason='已有快照保留；提交状态来自本次扫描', _contents=contents))
                parser = AttachmentParser(self.client.base)
                parser.feed(assignment.get('description') or '')
                for file_id in sorted(parser.ids):
                    try:
                        metadata = by_id.get(file_id) or self.client.get(f'/api/v1/files/{file_id}')
                        attachment_name = safe_name(metadata.get('display_name') or metadata.get('filename') or f'file-{file_id}')
                        self.add_file(course, metadata, target / self.layout['attachments'], course_dir, 'attachment', assignment['id'],
                                      ambiguous=name_counts.get(attachment_name.casefold(), 0)>1)
                    except ManagerError as error:
                        self.errors.append(dict(course_id=course['id'], section=f'attachment:{file_id}', error=str(error)))
        progress(stage='scan', current=len(self.courses), total=len(self.courses), message='扫描完成')
        return self.inventory()

    def inventory(self):
        return dict(items=[{k: v for k, v in item.items() if not k.startswith('_')} for item in self.items],
                    errors=self.errors, scanned_at=datetime.now(CST).isoformat(), courses=len(self.courses))

    def save_index(self):
        self.inside(self.index_path).parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.index-', suffix='.tmp', dir=self.index_path.parent)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(self.index, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, self.index_path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def download_selected(self, keys, progress=lambda **kw: None):
        by_key = {i['key']: i for i in self.items}
        if not keys or any(key not in by_key for key in keys):
            raise ManagerError('请选择本次扫描中的有效项目')
        selected = [by_key[key] for key in dict.fromkeys(keys)]
        # Protect the index against two independent CLI/UI processes downloading together.
        lock = self.inside(self.root / '.canvas-state' / 'download.lock')
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ManagerError('另一个下载任务正在运行；若异常退出，请确认无进程后移除 download.lock') from None
        results = []
        try:
            os.close(fd)
            if self.index_path.exists():
                self.index = json.loads(self.index_path.read_text(encoding='utf-8'))
            for number, item in enumerate(selected):
                progress(stage='download', current=number, total=len(selected), message=item['name'], bytes=0, size=item['size'])
                result = dict(key=item['key'], name=item['name'], path=item['path'])
                try:
                    if item['status'] == 'skipped':
                        result.update(status='skipped', reason=item['reason'])
                    elif item['kind'] == 'assignment':
                        target = self.inside(self.root / item['path'])
                        wrote = False
                        for name, content in item['_contents'].items():
                            path = self.inside(target / name)
                            if not path.exists():
                                try:
                                    atomic_new(path, [content.encode('utf-8')])
                                    wrote = True
                                except FileExistsError:
                                    pass
                        self.inside(target / self.layout['code']).mkdir(exist_ok=True)
                        result['status'] = 'downloaded' if wrote else 'exists'
                    else:
                        existing, reason = self.existing(item)
                        if existing:
                            result.update(status='exists', path=str(existing.relative_to(self.root)), reason=reason)
                        else:
                            path = self.inside(self.root / item['path'])
                            if path.exists():
                                stamp = hashlib.sha256(str(item.get('updated_at')).encode()).hexdigest()[:8]
                                path = path.with_name(f'{path.stem}__canvas-{item["file_id"]}-{stamp}{path.suffix}')
                            self.client.download(item['file_id'], path, item['size'],
                                                 lambda count: progress(bytes=count, size=item['size']), item.get('updated_at'))
                            self.index[item['identity']] = dict(path=str(path.relative_to(self.root)), size=path.stat().st_size,
                                                               updated_at=item.get('updated_at'), sha256=digest(path))
                            self.save_index()
                            result.update(status='downloaded', path=str(path.relative_to(self.root)))
                    item['status'] = 'exists' if result['status'] in ('exists', 'downloaded') else item['status']
                    item['path'] = result['path']
                except (ManagerError, OSError, ValueError) as error:
                    # Filesystem errors may contain paths, but never remote signed URLs.
                    result.update(status='failed', reason=str(error) if isinstance(error, ManagerError) else f'本地文件处理失败（{type(error).__name__}）')
                results.append(result)
                progress(current=number + 1, results=list(results))
        finally:
            lock.unlink(missing_ok=True)
        return results


class Application:
    def __init__(self, manager=None, config_path=None, profile=None):
        self.manager = manager
        self.config_path = Path(config_path) if config_path else Path.home()/'.canvas-manager/config.json'
        self.config = read_config(self.config_path) if config_path else {}
        self.profile = profile or (manager.client.profile if manager else None)
        self.preferences = public_settings(self.config, self.profile)
        if manager:
            self.preferences['settings']['root'] = str(manager.root)
        self.secret = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.state = dict(busy=False, stage='idle', current=0, total=0, message='点击扫描读取 Canvas', results=[])
        self.inventory = dict(items=[], errors=[])

    def public(self):
        return dict(**self.preferences, configured=self.manager is not None)

    def setup(self, action, data):
        with self.lock:
            if self.state['busy']:
                raise ManagerError('已有任务运行中，请完成后修改设置')
            self.state['busy'] = True
        try:
            if action == 'preview':
                return initialization_plan(data.get('settings', {}))
            config, profile = account_config(self.config, data)
            if action == 'test':
                client = CanvasClient(config, profile)
                client.probe()
                # Deliberately return only names and IDs, never the account token or full user profile.
                courses = client.get('/api/v1/courses', {'per_page':100, 'enrollment_state':'active'})
                return {'ok':True, 'courses':[{'id':c['id'], 'name':c['name']} for c in courses if c.get('name')]}
            settings = normalize_settings(data.get('settings', {}))
            manager = Manager(CanvasClient(config, profile), settings['root'], settings['course_map'], settings['max_mb'],
                              settings['include_media'], settings['all_courses'], settings['layout'], settings['course_ids'])
            plan = None
            if data.get('initialize'):
                if data.get('confirm_initialize') is not True:
                    raise SettingsError('请先预览并勾选确认创建新学期文件夹')
                plan = initialize_new(settings)
            config['canvas_profiles'][profile]['manager'] = settings
            try:
                save_config(self.config_path, config)
            except OSError:
                detail = '新资料库已创建；' if plan else ''
                raise SettingsError(detail+'配置保存失败，请检查配置目录权限；页面设置尚未切换') from None
            with self.lock:
                self.config, self.profile, self.manager = config, profile, manager
                self.preferences = public_settings(config, profile)
                self.inventory = dict(items=[], errors=[])
                self.state.update(message='配置已保存，请重新扫描', results=[])
            return {'ok':True, 'initialized':bool(plan), 'preferences':self.public()}
        finally:
            self.update(busy=False)

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def start(self, action, keys=None):
        with self.lock:
            if self.state['busy']:
                raise ManagerError('已有任务运行中')
            if self.manager is None:
                raise ManagerError('请先打开配置向导，填写 Canvas 账号和保存位置')
            if action == 'download' and (not isinstance(keys, list) or not keys or any(not isinstance(k, str) for k in keys)):
                raise ManagerError('下载项目列表无效')
            self.state = dict(busy=True, stage=action, current=0, total=0, message='任务启动', results=[])
        def run():
            try:
                if action == 'scan':
                    inventory = self.manager.scan(self.update)
                else:
                    self.manager.download_selected(keys, self.update)
                    inventory = self.manager.inventory()
                with self.lock:
                    self.inventory = inventory
                self.update(message='完成（如有失败请查看结果）')
            except Exception as error:
                self.update(message=str(error) if isinstance(error, ManagerError) else f'任务失败（{type(error).__name__}）')
                if action == 'scan':
                    with self.lock:
                        self.inventory = dict(items=[], errors=[{'error': self.state['message']}])
            finally:
                self.update(busy=False)
        threading.Thread(target=run, daemon=True).start()


def make_server(app, port=0):
    page = Path(__file__).with_name('canvas_manager.html').read_text(encoding='utf-8').replace('__CSRF__', app.secret)
    setup_page = Path(__file__).with_name('canvas_setup.html').read_text(encoding='utf-8').replace('__CSRF__', app.secret)
    assignments_page = Path(__file__).with_name('canvas_assignments.html').read_text(encoding='utf-8').replace('__CSRF__', app.secret)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, value, content_type='application/json; charset=utf-8'):
            payload = value if isinstance(value, bytes) else value.encode('utf-8') if isinstance(value, str) else json.dumps(value, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def valid_host(self):
            return self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}'

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {'error': 'Host 不允许'})
            if self.path in ('/', '/assignments'):
                return self.reply(200, assignments_page, 'text/html; charset=utf-8')
            if self.path == '/files':
                return self.reply(200, page, 'text/html; charset=utf-8')
            if self.path == '/setup':
                return self.reply(200, setup_page, 'text/html; charset=utf-8')
            if self.path == '/assets/canvas-token-settings.png':
                return self.reply(200, Path(__file__).with_name('assets').joinpath('canvas-token-settings.png').read_bytes(), 'image/png')
            if urlsplit(self.path).path == '/api/settings':
                with app.lock:
                    profile = parse_qs(urlsplit(self.path).query).get('profile', [None])[0]
                    try:
                        value = public_settings(app.config, profile) if profile else app.public()
                        return self.reply(200, value)
                    except SettingsError:
                        return self.reply(400, {'error':'账号设置无效，请检查配置文件'})
            if self.path == '/api/state':
                with app.lock:
                    payload = dict(state=dict(app.state), inventory=app.inventory, preferences=app.public())
                return self.reply(200, payload)
            self.reply(404, {'error': '未找到'})

        def do_POST(self):
            origin = self.headers.get('Origin')
            if not self.valid_host() or (origin and origin != f'http://127.0.0.1:{self.server.server_port}') or not secrets.compare_digest(self.headers.get('X-Canvas-CSRF', ''), app.secret):
                return self.reply(403, {'error': '请求校验失败'})
            if self.path not in ('/api/scan', '/api/download', '/api/setup/test', '/api/setup/preview', '/api/setup/save'):
                return self.reply(404, {'error': '未找到'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024 * 1024:
                    raise ValueError()
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError()
                if self.path.startswith('/api/setup/'):
                    return self.reply(200, app.setup(self.path.rsplit('/',1)[1], data))
                app.start('scan' if self.path == '/api/scan' else 'download', data.get('keys'))
                self.reply(202, {'ok': True})
            except (ValueError, ManagerError, SettingsError, OSError, TypeError, AttributeError) as error:
                self.reply(400, {'error': str(error) if isinstance(error, (ManagerError, SettingsError)) else '请求或配置文件格式无效，请检查输入'})
    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Canvas 课程文件、作业快照与提交进度；默认启动本地网页')
    parser.add_argument('command', choices=['serve', 'scan', 'download'], nargs='?', default='serve')
    user_config = Path.home()/'.canvas-manager/config.json'
    default_config = user_config if user_config.exists() or not (SOURCE/'config.json').exists() else SOURCE/'config.json'
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('CANVAS_CONFIG', str(default_config))))
    parser.add_argument('--profile')
    parser.add_argument('--root', type=Path)
    parser.add_argument('--course-map', type=Path, help='JSON: Canvas course ID → existing relative course directory')
    parser.add_argument('--max-mb', type=float)
    parser.add_argument('--include-media', action='store_true', default=None)
    parser.add_argument('--all-courses', action='store_true', default=None, help='包括当前账号可见的历史课程')
    parser.add_argument('--select', action='append', default=[], help='扫描输出的 key；可重复指定')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args(argv)
    try:
        config = read_config(args.config)
        preferences = public_settings(config, args.profile)
        settings = preferences['settings']
        for key in ('root', 'max_mb', 'include_media', 'all_courses'):
            value = getattr(args, key)
            if value is not None:
                settings[key] = str(value) if key=='root' else value
        settings = normalize_settings(settings)
        course_map = json.loads(args.course_map.read_text(encoding='utf-8')) if args.course_map else settings['course_map']
        if not isinstance(course_map, dict) or any(not isinstance(v, str) for v in course_map.values()):
            raise ManagerError('课程映射必须为 JSON 对象，值为目录字符串')
        manager = None
        if preferences['has_token']:
            manager = Manager(CanvasClient(config, args.profile), settings['root'], course_map, settings['max_mb'],
                              settings['include_media'], settings['all_courses'], settings['layout'], settings['course_ids'])
        if args.command == 'serve':
            app = Application(manager, args.config, args.profile)
            app.preferences['settings'] = settings
            server = make_server(app, args.port)
            url = f'http://127.0.0.1:{server.server_port}' + ('/assignments' if manager else '/setup')
            print(f'Canvas 管理器：{url}\nCtrl+C 停止。扫描只读；勾选后才下载。', flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            try:
                server.serve_forever()
            finally:
                server.server_close()
            return 0
        if manager is None:
            raise ManagerError('尚未配置账号；先运行 serve 并在配置向导填写 Token')
        inventory = manager.scan()
        if args.command == 'scan':
            print(json.dumps(inventory, ensure_ascii=False, indent=2))
            return 1 if inventory['errors'] else 0
        results = manager.download_selected(args.select, lambda **values: print(json.dumps(values, ensure_ascii=False), file=sys.stderr))
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 1 if inventory['errors'] or any(r['status'] == 'failed' for r in results) else 0
    except KeyboardInterrupt:
        return 130
    except (ManagerError, SettingsError, OSError, ValueError) as error:
        print(str(error) if isinstance(error, (ManagerError, SettingsError)) else f'启动失败（{type(error).__name__}）', file=sys.stderr)
        return 2


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    sys.exit(main())
