"""Portable settings, setup HTTP flow, semester safety and configurable download paths."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import canvas_manager as M
import canvas_settings as S
from test_canvas_manager import FakeCanvas


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.config=self.root/'private/config.json'

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, **values):
        return S.normalize_settings({'root':str(self.root/'new semester'), **values})

    def test_initialization_preview_is_readonly_and_creates_exact_layout(self):
        settings=self.settings(semester='2027 Spring', course_map={'1':'机器学习'}, layout={'slides':'讲义','homework':'练习','project':'项目','reference':'参考'})
        plan=S.initialization_plan(settings)
        self.assertFalse(Path(settings['root']).exists())
        self.assertEqual(len(plan['folders']),4)
        S.initialize_new(settings)
        root=Path(settings['root'])
        for path in plan['folders']:
            self.assertTrue(Path(path).is_dir())
        self.assertEqual(json.loads((root/'semester.json').read_text(encoding='utf-8'))['semester'],'2027 Spring')
        self.assertEqual((root/'.gitignore').read_text(),'.canvas-state/\n')

    def test_initialization_never_changes_existing_library(self):
        settings=self.settings()
        root=Path(settings['root']);root.mkdir();(root/'notes.txt').write_text('user work')
        with self.assertRaises(S.SettingsError):
            S.initialize_new(settings)
        self.assertEqual([p.name for p in root.iterdir()],['notes.txt'])
        self.assertEqual((root/'notes.txt').read_text(),'user work')

    def test_invalid_paths_names_and_duplicate_categories_rejected(self):
        for update in [{'root':'relative'}, {'layout':{'slides':'../escape'}}, {'layout':{'homework':'slides'}},
                       {'layout':{'code':'CON'}}, {'course_map':{'1':'a','2':'A'}}, {'max_mb':float('nan')},
                       {'course_ids':['not-an-id']}, {'labels':{'app_title':''}}]:
            with self.subTest(update=update),self.assertRaises(S.SettingsError):
                self.settings(**update)

    def test_private_config_preserves_unrelated_settings_and_redacts_token(self):
        existing={'other_app':'preserve','canvas_token':'legacy-secret'}
        config,profile=S.account_config(existing,{'profile':'student','token':'private-test-token','base_url':'https://canvas.example'})
        config['canvas_profiles'][profile]['manager']=self.settings()
        S.save_config(self.config,config)
        self.assertEqual(S.read_config(self.config)['other_app'],'preserve')
        self.assertNotIn('canvas_profiles',existing)
        public=json.dumps(S.public_settings(config))
        self.assertNotIn('private-test-token',public)
        self.assertNotIn('legacy-secret',public)

    def test_keep_saved_token_but_require_new_token_for_another_host(self):
        config,_=S.account_config({},dict(profile='a',token='private-test-token',base_url='https://canvas.example'))
        updated,_=S.account_config(config,dict(profile='a',token='',base_url='https://canvas.example'))
        self.assertEqual(updated['canvas_profiles']['a']['token'],'private-test-token')
        with self.assertRaises(S.SettingsError):
            S.account_config(config,dict(profile='a',token='',base_url='https://other.example'))
        with self.assertRaises(S.SettingsError):
            S.account_config(config,dict(profile='b',token='',base_url='https://canvas.example'))

    def test_account_settings_are_isolated(self):
        config,_=S.account_config({},dict(profile='a',token='a-token',base_url='https://canvas.example'))
        config['canvas_profiles']['a']['manager']=self.settings(semester='A')
        config,_=S.account_config(config,dict(profile='b',token='b-token',base_url='https://canvas.example'))
        config['canvas_profiles']['b']['manager']=self.settings(root=str(self.root/'B'),semester='B')
        self.assertEqual(S.public_settings(config,'a')['settings']['semester'],'A')
        self.assertEqual(S.public_settings(config,'b')['settings']['semester'],'B')
        fresh=S.public_settings(config,'new-account')
        self.assertEqual(fresh['settings']['course_ids'],[])
        self.assertFalse(fresh['has_token'])

    def test_placeholder_token_opens_setup_and_empty_account_is_rejected(self):
        self.assertFalse(S.public_settings({'canvas_token':'YOUR_CANVAS_API_TOKEN'})['has_token'])
        with self.assertRaises(S.SettingsError):
            S.account_config({},dict(profile='',token='token'))

    def test_token_urls_and_unsafe_canvas_addresses_rejected(self):
        for base in ['http://canvas.example','https://user:pass@canvas.example','https://canvas.example/?token=secret','https://canvas.example/path']:
            with self.subTest(base=base),self.assertRaises(S.SettingsError):
                S.account_config({},dict(profile='a',token='private',base_url=base))

    def test_custom_layout_applies_to_scan_download_and_index(self):
        api=FakeCanvas()
        layout={'slides':'讲义','homework':'练习','attachments':'材料','code':'答案','week_prefix':'周','date_assignments':False}
        manager=M.Manager(api,self.root,layout=layout,course_ids=['1'])
        report=manager.scan()
        file=next(i for i in report['items'] if i['key']=='1:file:0:11')
        assignment=next(i for i in report['items'] if i['kind']=='assignment')
        self.assertEqual(Path(file['path']),Path('机器学习/讲义/周02/Lecture 2.pdf'))
        self.assertEqual(Path(assignment['path']),Path('机器学习/练习/作业一'))
        results=manager.download_selected([file['key'],assignment['key']])
        self.assertTrue((self.root/results[1]['path']/'答案').is_dir())
        self.assertEqual([r['status'] for r in results],['downloaded','downloaded'])

    def test_selected_courses_filter_remote_inventory(self):
        manager=M.Manager(FakeCanvas(),self.root,course_ids=['999'])
        self.assertEqual(manager.scan()['items'],[])
        self.assertEqual(manager.courses,[])

    def test_save_reuses_existing_library_without_initialization(self):
        root=self.root/'existing';root.mkdir();(root/'user.txt').write_text('notes')
        app=M.Application(config_path=self.config)
        result=app.setup('save',dict(profile='student',token='private-test-token',base_url='https://canvas.example',settings=self.settings(root=str(root))))
        self.assertFalse(result['initialized'])
        self.assertEqual((root/'user.txt').read_text(),'notes')
        self.assertEqual(app.manager.root,root.resolve())
        self.assertFalse((root/'semester.json').exists())

    def test_initialization_requires_explicit_confirmation_and_writes_no_token(self):
        app=M.Application(config_path=self.config)
        data=dict(profile='student',token='private-test-token',base_url='https://canvas.example',settings=self.settings(),initialize=True)
        with self.assertRaises(S.SettingsError):
            app.setup('save',data)
        self.assertFalse(Path(data['settings']['root']).exists())
        self.assertFalse(self.config.exists())
        result=app.setup('save',{**data,'confirm_initialize':True})
        self.assertTrue(result['initialized'])
        self.assertNotIn('private-test-token',(Path(data['settings']['root'])/'semester.json').read_text())

    def test_settings_change_clears_old_inventory_and_busy_jobs_block_changes(self):
        app=M.Application(M.Manager(FakeCanvas(),self.root),config_path=self.config)
        app.inventory=app.manager.scan()
        app.state['busy']=True
        with self.assertRaises(M.ManagerError):
            app.setup('preview',{'settings':self.settings()})
        app.state['busy']=False
        app.setup('save',dict(profile='student',token='private-test-token',settings=self.settings()))
        self.assertEqual(app.inventory['items'],[])

    def test_test_connection_returns_no_credentials_or_personal_profile(self):
        app=M.Application(config_path=self.config)
        with patch.object(M.CanvasClient,'probe'),patch.object(M.CanvasClient,'get',return_value=[{'id':1,'name':'Course','private':'ignore'}]):
            result=app.setup('test',dict(profile='student',token='private-test-token'))
        self.assertEqual(result,{'ok':True,'courses':[{'id':1,'name':'Course'}]})
        self.assertFalse(self.config.exists())

    def test_new_install_has_setup_and_protected_http_endpoints(self):
        import requests
        app=M.Application(config_path=self.config)
        server=M.make_server(app)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        headers={'X-Canvas-CSRF':app.secret}
        try:
            for route in ['/setup','/assignments','/files']:
                r=requests.get(url+route,timeout=3)
                self.assertEqual(r.status_code,200)
                self.assertNotIn('__CSRF__',r.text)
            self.assertFalse(requests.get(url+'/api/settings',timeout=3).json()['configured'])
            self.assertEqual(requests.post(url+'/api/scan',headers=headers,json={},timeout=3).status_code,400)
            data=dict(profile='student',token='private-test-token',settings=self.settings(),initialize=True,confirm_initialize=True)
            self.assertEqual(requests.post(url+'/api/setup/save',json=data,timeout=3).status_code,403)
            r=requests.post(url+'/api/setup/save',headers=headers,json=data,timeout=3)
            self.assertEqual(r.status_code,200,r.text)
            self.assertNotIn('private-test-token',r.text)
            for endpoint in ['/api/settings','/api/state','/api/settings?profile=student']:
                self.assertNotIn('private-test-token',requests.get(url+endpoint,timeout=3).text)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_malformed_existing_config_is_preserved(self):
        self.config.parent.mkdir();self.config.write_text('{bad')
        with self.assertRaises(S.SettingsError):
            M.Application(config_path=self.config)
        self.assertEqual(self.config.read_text(),'{bad')

    def test_token_instruction_image_is_served_exactly_without_open_file_access(self):
        import requests
        app=M.Application(config_path=self.config)
        server=M.make_server(app)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        url=f'http://127.0.0.1:{server.server_port}'
        try:
            image=requests.get(url+'/assets/canvas-token-settings.png',timeout=3)
            expected=Path(M.__file__).with_name('assets')/'canvas-token-settings.png'
            self.assertEqual(image.status_code,200)
            self.assertEqual(image.headers['Content-Type'],'image/png')
            self.assertEqual(image.content,expected.read_bytes())
            self.assertIn('/assets/canvas-token-settings.png',requests.get(url+'/setup',timeout=3).text)
            self.assertEqual(requests.get(url+'/assets/config.json',timeout=3).status_code,404)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_built_zip_is_allowlisted_and_runs_after_clean_extraction(self):
        from build_canvas_package import build,FILES,ROOT
        output=build(self.root/'portable.zip')
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()),{'canvas-manager/'+p for p in FILES.values()})
            self.assertFalse(any(Path(n).name in ('config.json','token.json','index.json') for n in archive.namelist()))
            for name in archive.namelist():
                self.assertNotIn(b'C:\\Users\\Totheinfinity',archive.read(name))
            archive.extractall(self.root/'extracted')
        with self.assertRaises(FileExistsError):
            build(output)
        entry=self.root/'extracted/canvas-manager/run_canvas.py'
        result=subprocess.run([sys.executable,'-B',str(entry),'--help'],capture_output=True,text=True,encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)


if __name__=='__main__':
    unittest.main()
