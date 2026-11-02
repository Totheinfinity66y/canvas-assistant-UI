"""Portable configuration and optional, no-overwrite semester initialization."""
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


class SettingsError(Exception):
    pass


LAYOUT = dict(slides='slides', homework='homework', project='project', reference='reference',
              attachments='attachments', code='code', week_prefix='Week_', date_assignments=True)
LABELS = dict(app_title='Canvas 学习助手', assignments_title='作业进度', files_title='资料下载')


def read_config(path):
    if not Path(path).exists():
        return {}
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise SettingsError('配置文件无法读取；请修复或使用 --config 指定另一份配置，原文件未覆盖') from None


def component(value):
    if not isinstance(value, str) or not value or len(value) > 70 or value != value.strip('. ') or re.search(r'[<>:"/\\|?*\x00-\x1f]', value):
        raise SettingsError('目录名称必须为单个有效文件夹名，不能包含路径或特殊字符')
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I):
        raise SettingsError('目录名称不能使用 Windows 保留名称')
    return value


def normalize_settings(raw):
    if not isinstance(raw, dict):
        raise SettingsError('设置必须为对象')
    root = raw.get('root') or str(Path.home() / 'Downloads' / 'Canvas')
    if not isinstance(root, str) or not Path(root).expanduser().is_absolute():
        raise SettingsError('保存位置必须为本机绝对路径')
    root = Path(root).expanduser().resolve()
    if root == Path(root.anchor):
        raise SettingsError('请选择专门的资料库文件夹，不能使用磁盘根目录')
    layout = dict(LAYOUT)
    if not isinstance(raw.get('layout', {}), dict) or not isinstance(raw.get('labels', {}), dict):
        raise SettingsError('目录和页面命名设置必须为对象')
    layout.update({k:v for k,v in raw.get('layout', {}).items() if k in LAYOUT})
    for key in LAYOUT:
        if key != 'date_assignments':
            component(layout[key])
    if not isinstance(layout['date_assignments'], bool):
        raise SettingsError('作业日期设置必须为布尔值')
    # These pairs share a parent; equal names would mix different material types.
    for group in [('slides','homework'), ('project','reference'), ('attachments','code')]:
        if len({layout[k].casefold() for k in group}) != len(group):
            raise SettingsError('同层级的分类目录不能重名')
    labels = dict(LABELS)
    for key, value in raw.get('labels', {}).items():
        if key in labels:
            if not isinstance(value, str) or not value.strip() or len(value)>80:
                raise SettingsError('页面名称长度须为 1–80 字符')
            labels[key] = value.strip()
    course_map = raw.get('course_map') or {}
    if not isinstance(course_map, dict):
        raise SettingsError('课程映射必须为对象')
    for key, value in course_map.items():
        if not str(key).isdigit():
            raise SettingsError('课程映射的键须为 Canvas 课程 ID')
        component(value)
    if len({v.casefold() for v in course_map.values()}) != len(course_map):
        raise SettingsError('不同课程的目录不能重名')
    course_ids = raw.get('course_ids') or []
    if not isinstance(course_ids, list) or any(not str(i).isdigit() for i in course_ids):
        raise SettingsError('课程选择无效')
    try:
        maximum = float(raw.get('max_mb', 150))
    except (ValueError, TypeError):
        raise SettingsError('文件大小上限必须为正数') from None
    if not math.isfinite(maximum) or maximum <= 0:
        raise SettingsError('文件大小上限必须为正数')
    semester = raw.get('semester', '')
    if not isinstance(semester, str) or len(semester)>80:
        raise SettingsError('学期名称过长或格式错误')
    return dict(root=str(root), layout=layout, labels=labels, semester=semester.strip(), course_map=course_map,
                course_ids=list(dict.fromkeys(str(i) for i in course_ids)), max_mb=maximum,
                include_media=bool(raw.get('include_media', False)), all_courses=bool(raw.get('all_courses', False)))


def account_config(existing, data):
    if 'profile' in data and not data['profile']:
        raise SettingsError('请填写用于区分账号的名称')
    profile = data.get('profile') or existing.get('canvas_default_profile') or 'default'
    if not isinstance(profile, str) or not re.fullmatch(r'[\w-]{1,60}', profile):
        raise SettingsError('账号名请使用字母、数字、汉字、下划线或连字符')
    profiles = existing.get('canvas_profiles') or {}
    saved = profiles.get(profile) or (existing if profile==(existing.get('canvas_default_profile') or 'default') and not profiles else {})
    base = (data.get('base_url') or saved.get('base_url') or 'https://oc.sjtu.edu.cn').rstrip('/')
    parts = urlsplit(base)
    if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('', '/'):
        raise SettingsError('Canvas 地址须为 HTTPS 网站根地址，不带账号、查询参数或路径')
    token = data.get('token') or saved.get('token') or saved.get('canvas_token')
    saved_base = (saved.get('base_url') or existing.get('base_url') or 'https://oc.sjtu.edu.cn').rstrip('/')
    if not data.get('token') and saved_base != base:
        raise SettingsError('更换 Canvas 网站时请重新输入 Token')
    if not isinstance(token, str) or not token.strip() or token.startswith('YOUR_') or '\n' in token or '\r' in token:
        raise SettingsError('请填写有效的 Canvas Token；已保存账号可留空保留')
    # Do not mutate callers' configurations or return any credential in public settings.
    updated = dict(existing)
    updated['canvas_profiles'] = {**profiles, profile: dict(token=token.strip(), base_url=base)}
    updated['canvas_default_profile'] = profile
    return updated, profile


def public_settings(config, profile=None):
    profile = profile or config.get('canvas_default_profile') or 'default'
    saved = (config.get('canvas_profiles') or {}).get(profile, {})
    legacy = config.get('manager') if not config.get('canvas_profiles') else None
    settings = normalize_settings(saved.get('manager') or legacy or {'root':os.environ.get('CANVAS_DEFAULT_ROOT') or config.get('save_dir')})
    token = saved.get('token') or (config.get('canvas_token') if not config.get('canvas_profiles') else None)
    return dict(settings=settings, profile=profile, profiles=list((config.get('canvas_profiles') or {}).keys()),
                base_url=saved.get('base_url') or config.get('base_url') or 'https://oc.sjtu.edu.cn',
                has_token=isinstance(token, str) and bool(token.strip()) and not token.startswith('YOUR_'))


def initialization_plan(settings):
    settings = normalize_settings(settings)
    root, layout = Path(settings['root']), settings['layout']
    folders = []
    for course_id, directory in settings['course_map'].items():
        if settings['course_ids'] and str(course_id) not in settings['course_ids']:
            continue
        for suffix in [layout['slides'], f"{layout['slides']}/{layout['project']}",
                       f"{layout['slides']}/{layout['reference']}", layout['homework']]:
            folders.append(str(root / directory / suffix))
    return dict(root=str(root), exists=root.exists(), folders=folders,
                files=[str(root/'semester.json'), str(root/'.gitignore')])


def initialize_new(settings):
    """Only a completely new root may be initialized; never migrate existing files."""
    plan = initialization_plan(settings)
    root = Path(plan['root'])
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise SettingsError('初始化仅支持尚不存在的新文件夹；原资料库未修改') from None
    for folder in plan['folders']:
        Path(folder).mkdir(parents=True, exist_ok=False)
    (root/'.gitignore').write_text('.canvas-state/\n', encoding='utf-8')
    # Contains display/layout information only, never account credentials.
    (root/'semester.json').write_text(json.dumps(normalize_settings(settings), ensure_ascii=False, indent=2), encoding='utf-8')
    return plan


def save_config(path, config):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.canvas-config-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(config, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
