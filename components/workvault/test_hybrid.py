"""Guarded adapter tests use synthetic canonical cards and disposable children."""
import fcntl
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import os
from pathlib import Path
import select
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import batch
import curation
import lifecycle
import sync
import workvault
from test_retrieval import publication

try:
    import hybrid
except ImportError:
    hybrid = None


FAKE_CLI = '''#!{python}
import json, os, pathlib, sqlite3, sys, time
root = pathlib.Path(os.environ['OBSIDIAN_VAULT_PATH'])
args = sys.argv[1:]
calls = pathlib.Path(__file__ + '.calls')
with calls.open('a') as stream:
    stream.write(json.dumps({{'args':args, 'cwd':os.getcwd(), 'env':dict(os.environ)}}) + '\\n')
mode = {mode!r}
if mode == 'sleep':
    pathlib.Path(__file__ + '.pid').write_text(str(os.getpid()))
    time.sleep(10)
if 'reindex' in args:
    db = root / '.obsidian-hybrid-search.db'
    conn = sqlite3.connect(db)
    conn.executescript("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE notes(id INTEGER PRIMARY KEY,path TEXT); CREATE TABLE chunks(id INTEGER PRIMARY KEY,note_id INTEGER,embedding_status TEXT); CREATE TABLE links(source_id INTEGER,target_id INTEGER);")
    conn.executemany('INSERT INTO settings VALUES (?,?)', [('embedding_model','local:Xenova/multilingual-e5-small'),('embedding_dim','384'),('vault_path',str(root)),('api_base_url','https://api.openai.com/v1'),('api_model','text-embedding-3-small'),('last_indexed','2026-10-08T00:00:00Z')])
    names = sorted(p.name for p in root.glob('*.md'))
    conn.executemany('INSERT INTO notes VALUES (?,?)', enumerate(names,1))
    conn.executemany('INSERT INTO chunks VALUES (?,?,?)', [(i,i,'ok') for i in range(1,len(names)+1)])
    conn.commit()
    conn.close()
    db.chmod(0o600)
    if mode == 'fail':
        sys.stderr.write('SECRET_CHILD_STDERR')
        sys.exit(1)
    print('indexed')
elif 'search' in args:
    if mode == 'badjson':
        print('SECRET_BODY malformed stdout')
    elif mode == 'oversize':
        print('x' * (1024*1024+10))
    else:
        conn = sqlite3.connect(root / '.obsidian-hybrid-search.db')
        if '--path' in args:
            name = conn.execute('SELECT path FROM notes WHERE path=?', (args[args.index('--path') + 1],)).fetchone()[0]
        else:
            name = conn.execute('SELECT path FROM notes ORDER BY path LIMIT 1').fetchone()[0]
        conn.close()
        if mode == 'unknown': name = 'Human.md'
        if mode == 'badresult':
            print(json.dumps([{{'path':name,'score':'SECRET_MALFORMED_RESULT'}}]))
        else:
            print(json.dumps([{{'path':name,'title':'Synthetic evidence','tags':[],'aliases':[],'score':0.9,'snippet':'Uncertain synthetic evidence','previewAnchors':[{{'kind':'bm25','headingPath':'Evidence','matchText':'Uncertain','charStart':0,'charEnd':9}}]}}]))
'''


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(hybrid, 'guarded hybrid adapter is missing')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.home = self.root / 'home'
        self.home.mkdir(mode=0o700)
        home_patch = patch.object(Path, 'home', return_value=self.home)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        self.config = sync.SyncConfig('example/wiki', self.root / 'vault', self.root / 'history',
            self.root / 'state', 'synthetic', 'eu', 'synthetic')
        for path in (self.config.vault, self.config.state): path.mkdir(mode=0o700)
        self.paths = ['knowledge/item-1.md', 'knowledge/item-2.md']
        self.state(self.paths)
        with workvault._publication_lock(self.config.vault, required=True): pass
        self.put(publication())
        self.put(publication(2, commit='2' * 40))
        self.cli = self.root / 'synthetic-cli'
        self.set_cli()
        private = self.home / '.config' / 'workvault'
        private.mkdir(parents=True, mode=0o700)
        cache = self.home / '.cache' / 'huggingface'
        files = []
        for name in ('config.json','tokenizer.json','tokenizer_config.json','onnx/model_quantized.onnx'):
            relative = 'Xenova/multilingual-e5-small/' + name
            path = cache / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = (json.dumps({'model_type':'bert','hidden_size':384,'architectures':['BertModel']}).encode()
                   if name == 'config.json' else ('SYNTHETIC_CACHE_' + name).encode())
            path.write_bytes(raw)
            files.append({'path':relative,'bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()})
        self.manifest = private / 'hybrid-model-cache.json'
        self.manifest.write_text(json.dumps({'schema_version':'workvault-hybrid-model-cache-v1',
            'local_model':'Xenova/multilingual-e5-small','cache_root':str(cache),'files':files}))
        self.manifest.chmod(0o600)
        self.binding = {'schema_version':'workvault-hybrid-binding-v1','workvault_root':str(self.config.vault),
            'cli_path':str(self.cli),'cli_artifact_sha256':hashlib.sha256(self.cli.read_bytes()).hexdigest(),
            'cli_version':'0.15.6','local_model':'Xenova/multilingual-e5-small',
            'model_cache_manifest_sha256':hashlib.sha256(self.manifest.read_bytes()).hexdigest()}
        self.binding_file = private / 'hybrid.json'
        self.binding_file.write_text(json.dumps(self.binding))
        self.binding_file.chmod(0o600)

    def state(self, paths):
        target = self.config.state / sync.STATE
        target.write_text(json.dumps({'schema_version':sync.SCHEMA,'repository':self.config.repository,
            'target_commit':'2'*40,'discovered_paths':paths,'indexed_paths':paths,'tracked_paths':self.paths,
            'failed_paths':[],'uncertain_paths':[],'provider_evidence':{'outcome':'not_started'},'broken_index_references':[]}))
        target.chmod(0o600)

    def put(self, value):
        name = batch.publication_name(value['batch']['repository'], value['batch']['cards'][0]['path'])
        target = self.config.vault / name
        target.write_bytes(curation.render_publication(value['batch'],value['published_at']))
        target.chmod(0o600)
        return name

    def set_cli(self, mode='ok'):
        self.cli.write_text(FAKE_CLI.format(python=sys.executable,mode=mode))
        self.cli.chmod(0o700)
        if hasattr(self,'binding'):
            self.binding['cli_artifact_sha256'] = hashlib.sha256(self.cli.read_bytes()).hexdigest()

    def calls(self):
        target = Path(str(self.cli)+'.calls')
        return [json.loads(row) for row in target.read_text().splitlines()] if target.exists() else []

    def seal(self):
        with workvault._publication_lock(self.config.vault, expose_lock=True, required=True, lifecycle=True) as (directory,_):
            row = lifecycle._artifacts(directory)[lifecycle.INDEX]
            row['members'][0]['info'] = lifecycle._index_record(directory,lifecycle.INDEX_DB)
            lifecycle._save(directory,{lifecycle.INDEX:row},lifecycle.ARTIFACTS)

    def test_binding_is_private_strict_and_pins_artifact(self):
        self.assertEqual(hybrid.load_binding(self.binding_file),self.binding)
        for damage in ('mode','extra','artifact','version'):
            with self.subTest(damage=damage):
                data = dict(self.binding)
                if damage=='extra': data['endpoint']='https://remote.invalid'
                if damage=='version': data['cli_version']='latest'
                if damage=='artifact': data['cli_artifact_sha256']='0'*64
                self.binding_file.write_text(json.dumps(data))
                self.binding_file.chmod(0o644 if damage=='mode' else 0o600)
                with self.assertRaises(workvault.WorkVaultError): hybrid.load_binding(self.binding_file)

    def test_reindex_constructs_environment_pins_root_and_search_preserves_citations(self):
        with patch.dict(os.environ,{'OPENAI_BASE_URL':'https://remote.invalid','OPENAI_API_KEY':'SECRET',
                'NODE_OPTIONS':'--require poisoned','OBSIDIAN_VAULT_PATH':'/wrong','OBSIDIAN_INCLUDE_PATTERNS':'Human.md'}):
            hybrid.reindex(self.config,self.binding)
            results = hybrid.search(self.config,self.binding,'carrier',{'anchors':True,'snippetLength':400,'limit':5})
        self.assertEqual(len(results),1)
        self.assertIn('reference',results[0]); self.assertIn('citation',results[0])
        self.assertEqual(results[0]['previewAnchors'][0]['matchText'],'Uncertain')
        call = self.calls()[-1]
        self.assertEqual(call['cwd'],str(self.config.vault))
        self.assertEqual(call['args'][:3],['--db',str(self.config.vault/lifecycle.INDEX_DB),'search'])
        self.assertEqual(call['args'][-3:],['--json','--','carrier'])
        self.assertIn('--anchors',call['args']); self.assertIn('400',call['args'])
        self.assertEqual(call['env']['OBSIDIAN_VAULT_PATH'],str(self.config.vault))
        self.assertFalse(any(key.startswith('OPENAI_') for key in call['env']))
        self.assertNotIn('NODE_OPTIONS',call['env']); self.assertNotIn('OBSIDIAN_INCLUDE_PATTERNS',call['env'])
        self.assertEqual(call['env']['LOCAL_EMBEDDING_MODEL'],'Xenova/multilingual-e5-small')
        self.assertFalse((self.config.vault/lifecycle.INDEX).exists())
        self.assertFalse(any((self.config.vault/name).exists() for name in lifecycle.INDEX_FILES[1:]))

    def test_build_contains_all_166_active_cards_without_human_or_retired(self):
        for index in range(3,168):
            value = publication(index)
            self.paths.append(value['batch']['cards'][0]['path']); self.put(value)
        self.state(self.paths[1:])
        human = self.config.vault/'Human.md'; human.write_text('PRIVATE_HUMAN_CANARY')
        hybrid.reindex(self.config,self.binding)
        conn = sqlite3.connect(self.config.vault/lifecycle.INDEX_DB)
        try: names = {row[0] for row in conn.execute('SELECT path FROM notes')}
        finally: conn.close()
        self.assertEqual(len(names),166)
        self.assertIn(batch.publication_name('example/wiki','knowledge/item-167.md'),names)
        self.assertNotIn(batch.publication_name('example/wiki','knowledge/item-1.md'),names)
        self.assertNotIn('Human.md',names)
        self.assertEqual(human.read_text(),'PRIVATE_HUMAN_CANARY')

    def test_missing_changed_or_empty_cache_launches_no_child(self):
        cache_file = self.home/'.cache/huggingface/Xenova/multilingual-e5-small/config.json'
        cache_file.unlink()
        with self.assertRaises(workvault.WorkVaultError): hybrid.reindex(self.config,self.binding)
        self.assertEqual(self.calls(),[])
        value=json.loads(self.manifest.read_text()); value['files']=[]
        self.manifest.write_text(json.dumps(value))
        self.binding['model_cache_manifest_sha256']=hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        with self.assertRaises(workvault.WorkVaultError): hybrid.reindex(self.config,self.binding)
        self.assertEqual(self.calls(),[])

    def test_remote_stored_settings_unknown_notes_or_ready_wal_refuse_before_child(self):
        hybrid.reindex(self.config,self.binding)
        count=len(self.calls())
        db=self.config.vault/lifecycle.INDEX_DB
        conn=sqlite3.connect(db); conn.execute("UPDATE settings SET value='https://remote.invalid' WHERE key='api_base_url'"); conn.commit(); conn.close()
        self.seal()
        with self.assertRaises(workvault.WorkVaultError): hybrid.search(self.config,self.binding,'carrier')
        self.assertEqual(len(self.calls()),count)
        conn=sqlite3.connect(db); conn.execute("UPDATE settings SET value='https://api.openai.com/v1' WHERE key='api_base_url'"); conn.commit(); conn.close(); self.seal()
        wal=self.config.vault/(lifecycle.INDEX_DB+'-wal'); wal.write_bytes(b'REMOTE_URL_WAL_CANARY'); wal.chmod(0o600)
        with self.assertRaises(workvault.WorkVaultError): hybrid.search(self.config,self.binding,'carrier')
        self.assertEqual(len(self.calls()),count)
        self.assertEqual(wal.read_bytes(),b'REMOTE_URL_WAL_CANARY')

    def test_stale_generation_and_unfinished_family_never_search(self):
        hybrid.reindex(self.config,self.binding)
        count=len(self.calls()); self.state(self.paths[:1])
        with self.assertRaises(workvault.WorkVaultError): hybrid.search(self.config,self.binding,'carrier')
        self.assertEqual(len(self.calls()),count)
        self.state(self.paths)
        with workvault._publication_lock(self.config.vault,expose_lock=True,required=True) as (directory,_):
            lifecycle.register_owned_index(directory,lifecycle._artifacts(directory)[lifecycle.INDEX]['generation'],'inflight',[{'scope':'vault','name':lifecycle.INDEX_DB}])
        with self.assertRaises(workvault.WorkVaultError): hybrid.search(self.config,self.binding,'carrier')
        self.assertEqual(len(self.calls()),count)

    def test_invalid_options_query_and_result_paths_refuse_without_disclosure(self):
        hybrid.reindex(self.config,self.binding); count=len(self.calls())
        for options in ({'limit':0},{'rerank':True},{'related':True},{'notePath':'Human.md'}, {'snippetLength':401},{'scope':['ok',3]},{'mode':'graph'},{'anchors':1}):
            with self.subTest(options=options):
                with self.assertRaises(workvault.WorkVaultError): hybrid.search(self.config,self.binding,'carrier',options)
        self.assertEqual(len(self.calls()),count)
        for mode in ('badjson','badresult','unknown','oversize'):
            self.set_cli(mode)
            # Changing artifact changes generation; explicitly build the reviewed new synthetic executable.
            hybrid.reindex(self.config,self.binding)
            with self.assertRaises(workvault.WorkVaultError) as caught: hybrid.search(self.config,self.binding,'carrier')
            self.assertNotIn('SECRET',str(caught.exception))

    def test_complete_ui_search_status_and_oversize_request_stream_continues(self):
        hybrid.reindex(self.config,self.binding)
        output=io.StringIO()
        requests='x'*(16*1024+1)+'\n'+json.dumps({'id':'ui-search-1','query':'carrier','options':
            {'mode':'hybrid','limit':5,'threshold':0,'scope':[],'tag':[],'frontmatter':[],
             'snippetLength':400,'anchors':True,'rerank':False}})+'\n'
        # The actual UI can omit filters; empty filter arrays are valid no-ops.
        requests += json.dumps({'id':'ui-status-2','action':'status'})+'\n'
        hybrid.serve(self.config,self.binding,io.StringIO(requests),output)
        rows=[json.loads(row) for row in output.getvalue().splitlines()]
        self.assertIn('error',rows[1]); self.assertEqual(rows[2]['id'],'ui-search-1')
        self.assertIn('results',rows[2]); self.assertEqual(rows[2]['results'][0]['title'],'Synthetic evidence')
        self.assertEqual(rows[3]['id'],'ui-status-2'); self.assertEqual(rows[3]['status']['index_state'],'ready')

    def test_model_config_changed_layout_refuses_even_with_reviewed_manifest(self):
        path=self.home/'.cache/huggingface/Xenova/multilingual-e5-small/config.json'
        raw=json.dumps({'model_type':'bert','hidden_size':384,'architectures':['BertModel'],
            'transformers.js_config':{'use_external_data_format':True}}).encode()
        path.write_bytes(raw)
        value=json.loads(self.manifest.read_text())
        for row in value['files']:
            if row['path'].endswith('/config.json'):
                row.update(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())
        self.manifest.write_text(json.dumps(value))
        self.binding['model_cache_manifest_sha256']=hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        with self.assertRaises(workvault.WorkVaultError): hybrid.reindex(self.config,self.binding)
        self.assertEqual(self.calls(),[])

    def test_status_is_read_only_without_cache_or_native_status(self):
        hybrid.reindex(self.config,self.binding)
        before=(self.config.vault/lifecycle.INDEX_DB).read_bytes(); count=len(self.calls())
        self.manifest.unlink()
        result=hybrid.status(self.config,self.binding)
        self.assertEqual((result['total'],result['indexed'],result['chunks'],result['failed_chunks']),(2,2,2,0))
        self.assertEqual(result.get('model'),'local:Xenova/multilingual-e5-small')
        self.assertEqual(result['active_model'],'local:Xenova/multilingual-e5-small')
        self.assertEqual(result['embedding_dim'],384)
        self.assertEqual(result['version'],'0.15.6')
        self.assertEqual(result['api_base_url'],None); self.assertEqual(result['index_state'],'ready')
        self.assertEqual(before,(self.config.vault/lifecycle.INDEX_DB).read_bytes())
        self.assertEqual(len(self.calls()),count)

    def test_ui_empty_query_managed_note_path_reaches_guarded_child(self):
        hybrid.reindex(self.config,self.binding)
        count=len(self.calls())
        name=batch.publication_name('example/wiki','knowledge/item-2.md')
        request={'id':'ui-note-preview','query':'','options':
            {'notePath':name,'anchors':True,'snippetLength':400}}
        output=io.StringIO()
        self.assertEqual(hybrid.serve(self.config,self.binding,io.StringIO(json.dumps(request)+'\n'),output),0)
        rows=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[0],{'ready':True})
        self.assertEqual(rows[1]['id'],'ui-note-preview')
        self.assertEqual([result['path'] for result in rows[1]['results']],[name])
        self.assertIn('reference',rows[1]['results'][0])
        self.assertIn('citation',rows[1]['results'][0])
        self.assertEqual(len(self.calls()),count+1)
        argv=self.calls()[-1]['args']
        self.assertEqual(argv[argv.index('--path')+1],name)
        self.assertEqual(argv[-3:],['--json','--',''])
        self.assertEqual(hybrid.status(self.config,self.binding)['index_state'],'ready')

    def test_empty_or_whitespace_query_without_note_path_launches_no_child(self):
        for query in ('','   '):
            with self.subTest(query=query):
                self.assertEqual(hybrid.search(self.config,self.binding,query,{'anchors':True,'snippetLength':400}),[])
        self.assertEqual(self.calls(),[])

    def test_ui_request_shapes_safe_ids_empty_warmup_and_sanitized_errors(self):
        output=io.StringIO()
        requests='\n'.join([json.dumps({'id':7,'query':''}),json.dumps({'id':'status-1','action':'status'}),
            json.dumps({'id':8,'query':'carrier','options':{'anchors':True,'snippetLength':400}}),
            json.dumps({'id':9,'query':'carrier','options':{'rerank':True}}),'SECRET malformed'])+'\n'
        self.assertEqual(hybrid.serve(self.config,self.binding,io.StringIO(requests),output),0)
        rows=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(rows[0],{'ready':True}); self.assertEqual(rows[1],{'id':7,'results':[]})
        self.assertEqual(rows[2]['id'],'status-1'); self.assertEqual(rows[2]['status']['index_state'],'absent')
        self.assertEqual(rows[3]['id'],8); self.assertIn('error',rows[3])
        self.assertNotIn('SECRET',output.getvalue()); self.assertEqual(self.calls(),[])

    def test_sync_dispatch_hybrid_actions_preserves_existing_consumption(self):
        config_file=self.root/'sync.json'
        config_file.write_text(json.dumps({'schema_version':'workvault-sync-config-v1','repository':'example/wiki',
            'vault':str(self.config.vault),'snapshots':str(self.config.snapshots),'state':str(self.config.state),
            'vertex':{'project':'synthetic','location':'eu','model':'synthetic'}})); config_file.chmod(0o600)
        def invoke(action, *args):
            output=io.StringIO()
            with redirect_stdout(output),redirect_stderr(io.StringIO()):
                try: code=sync.main([action,'--config',str(config_file),*args])
                except SystemExit as error: code=error.code
            return code, output.getvalue()
        code,output=invoke('hybrid-reindex','--hybrid-config',str(self.binding_file))
        self.assertEqual(code,0, 'explicit hybrid command dispatch is missing')
        self.assertEqual(json.loads(output)['total'],2)
        code,output=invoke('hybrid-search','--hybrid-config',str(self.binding_file),'--limit','2','--','carrier')
        self.assertEqual(code,0); self.assertIn('citation',json.loads(output)[0])
        self.assertEqual(invoke('search','--','carrier')[0],0)
        self.assertNotEqual(invoke('hybrid-search','--','carrier')[0],0)

    def test_status_reports_stale_and_recovery_required_without_mutation(self):
        hybrid.reindex(self.config,self.binding)
        self.state(self.paths[:1])
        self.assertEqual(hybrid.status(self.config,self.binding)['index_state'],'stale')
        self.state(self.paths)
        with workvault._publication_lock(self.config.vault,required=True,expose_lock=True) as (directory,_):
            generation=lifecycle._artifacts(directory)[lifecycle.INDEX]['generation']
            lifecycle.register_owned_index(directory,generation,'inflight',[{'scope':'vault','name':lifecycle.INDEX_DB}])
        before={p.name:p.read_bytes() for p in self.config.vault.iterdir() if p.is_file()}
        self.assertEqual(hybrid.status(self.config,self.binding)['index_state'],'recovery_required')
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.config.vault.iterdir() if p.is_file()})

    def test_actual_wal_only_remote_setting_refuses_without_child_or_erasure(self):
        hybrid.reindex(self.config,self.binding)
        db=self.config.vault/lifecycle.INDEX_DB
        conn=sqlite3.connect(db)
        try:
            conn.execute('PRAGMA journal_mode=WAL'); conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            conn.execute("UPDATE settings SET value='https://wal-only.invalid' WHERE key='api_base_url'"); conn.commit()
            immutable=sqlite3.connect(db.as_uri()+'?immutable=1',uri=True)
            try: self.assertEqual(immutable.execute("SELECT value FROM settings WHERE key='api_base_url'").fetchone()[0],'https://api.openai.com/v1')
            finally: immutable.close()
            self.assertEqual(conn.execute("SELECT value FROM settings WHERE key='api_base_url'").fetchone()[0],'https://wal-only.invalid')
            wal=Path(str(db)+'-wal'); before=wal.read_bytes(); count=len(self.calls())
            with self.assertRaises(workvault.WorkVaultError): hybrid.search(self.config,self.binding,'carrier')
            self.assertEqual(len(self.calls()),count); self.assertEqual(wal.read_bytes(),before)
        finally: conn.close()

    def test_changed_cache_and_mismatched_binding_root_refuse_without_launch(self):
        path=self.home/'.cache/huggingface/Xenova/multilingual-e5-small/tokenizer.json'
        path.write_bytes(b'CHANGED_CACHE_CANARY')
        with self.assertRaises(workvault.WorkVaultError): hybrid.reindex(self.config,self.binding)
        other=self.root/'other'; other.mkdir(mode=0o700)
        wrong=dict(self.binding,workvault_root=str(other))
        with self.assertRaises(workvault.WorkVaultError): hybrid.reindex(self.config,wrong)
        self.assertEqual(self.calls(),[])

    def test_mixed_v1_v2_generation_retains_exact_rows_and_unknown_database_note_refuses(self):
        from test_source_sync import Adapter, Curator, CUTOFF, config, selection
        sync.sync_sources(config(self.root),selections=(selection(Adapter()),),cutoff=CUTOFF,curator=Curator())
        result=hybrid.reindex(self.config,self.binding)
        self.assertEqual(result['total'],3)
        with sync._curated_reader(self.config) as (reader,_):
            rows=reader.rows()
            self.assertEqual({row['reference']['commit'] for row in rows if 'commit' in row['reference']},{'1'*40,'2'*40})
            self.assertEqual(next(row['reference']['kind'] for row in rows if 'kind' in row['reference']),'slack')
            self.assertEqual(hybrid.collection_generation(reader,self.binding),result['generation'])
            reordered=hybrid.retrieval.CuratedReader([publication(2,commit='2'*40),publication()])
            previous=hybrid.retrieval.CuratedReader([publication(),publication(2,commit='2'*40)])
            self.assertEqual(hybrid.collection_generation(previous,self.binding),hybrid.collection_generation(reordered,self.binding))
        db=self.config.vault/lifecycle.INDEX_DB
        conn=sqlite3.connect(db)
        conn.execute("INSERT INTO notes VALUES (99,'Human.md')"); conn.commit(); conn.close(); self.seal()
        count=len(self.calls())
        with self.assertRaises(workvault.WorkVaultError): hybrid.search(self.config,self.binding,'carrier')
        self.assertEqual(len(self.calls()),count)

    def test_failed_reindex_erases_reserved_database_and_projection_canaries(self):
        self.set_cli('fail')
        with self.assertRaises(workvault.WorkVaultError): hybrid.reindex(self.config,self.binding)
        self.assertFalse((self.config.vault/lifecycle.INDEX).exists())
        self.assertFalse(any((self.config.vault/name).exists() for name in lifecycle.INDEX_FILES))
        with workvault._publication_lock(self.config.vault,required=True) as directory:
            self.assertNotIn(lifecycle.INDEX,lifecycle._artifacts(directory))

    def test_post_reader_state_drift_discards_buffered_results(self):
        hybrid.reindex(self.config,self.binding)
        original=hybrid._run_child
        def drift(*args,**kwargs):
            result=original(*args,**kwargs); self.state(self.paths[:1]); return result
        with patch.object(hybrid,'_run_child',side_effect=drift):
            with self.assertRaises(workvault.WorkVaultError): hybrid.search(self.config,self.binding,'carrier')

    def test_post_child_executable_drift_discards_buffered_results(self):
        hybrid.reindex(self.config,self.binding)
        original=hybrid._run_child
        def drift(*args,**kwargs):
            result=original(*args,**kwargs)
            self.cli.write_text(self.cli.read_text()+'\n# changed artifact\n')
            return result
        with patch.object(hybrid,'_run_child',side_effect=drift):
            with self.assertRaises(workvault.WorkVaultError): hybrid.search(self.config,self.binding,'carrier')

    def test_real_timeout_reaps_child_before_family_erasure(self):
        self.set_cli('sleep')
        with patch.object(hybrid,'REINDEX_TIMEOUT',0.5):
            with self.assertRaises(workvault.WorkVaultError): hybrid.reindex(self.config,self.binding)
        pid=int(Path(str(self.cli)+'.pid').read_text())
        with self.assertRaises(ProcessLookupError): os.kill(pid,0)
        self.assertFalse((self.config.vault/lifecycle.INDEX).exists())

    def test_parent_death_child_retains_real_kernel_lease(self):
        command='''import pathlib,sys
sys.path.insert(0,sys.argv[1])
import hybrid,workvault
root=pathlib.Path(sys.argv[2])
with workvault._publication_lock(root,required=True,expose_lock=True) as (_,lock):
 hybrid._run_child([sys.executable,'-c',"import os,pathlib,sys,time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(1.5)",sys.argv[3]],root,{},lock,5)
'''
        # Direct disposable process tests the adapter's inheritance and abnormal-parent path.
        started=self.root/'lease-child.pid'
        parent=subprocess.Popen([sys.executable,'-B','-c',command,str(Path(__file__).parent),str(self.config.vault),str(started)],stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+5
            while not started.exists():
                self.assertLess(time.monotonic(),deadline,'lease child did not start'); time.sleep(0.01)
            parent.kill(); parent.wait(timeout=5)
            with (self.config.vault/workvault.LOCK).open('rb') as lock:
                with self.assertRaises(BlockingIOError): fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                deadline=time.monotonic()+5
                while True:
                    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB); break
                    except BlockingIOError:
                        self.assertLess(time.monotonic(),deadline); time.sleep(0.05)
        finally:
            if parent.poll() is None: parent.kill(); parent.wait(timeout=5)
            parent.stdout.close(); parent.stderr.close()


if __name__ == '__main__': unittest.main()
