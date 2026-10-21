"""The normal probe owns one fresh child and preserves default Host identity."""
import contextlib
import hashlib
import hmac
import http.server
import importlib.util
import json
import os
import pathlib
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SUPPORT = ROOT / 'components/skills/maka_native_host.py'


class OwnedNormalHostTests(unittest.TestCase):
    def module(self):
        self.assertTrue(SUPPORT.is_file(), 'portable owned normal Host helper is missing')
        sys.path.insert(0, str(SUPPORT.parent))
        spec = importlib.util.spec_from_file_location('maka_native_host', SUPPORT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_real_owned_host_and_native_probe_use_only_authenticated_nonforwarding_fixture(self):
        support=self.module()
        token, attestation=secrets.token_urlsafe(36),secrets.token_urlsafe(36)
        observations=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                observations.append(self.path)
                if self.path=='/healthz':
                    challenge=self.headers['X-Maka-Relay-Challenge']
                    value={'status':'ready','proof':hmac.new(attestation.encode(),b'maka-relay-health-v1\0'+challenge.encode(),hashlib.sha256).hexdigest()}
                elif self.path=='/v1/models' and self.headers['Authorization']=='Bearer '+token:
                    value={'data':[{'id':name} for name in ['gpt-6.1-sol','gpt-6-sol','gpt-5.6-sol']]}
                else: self.send_error(403);return
                raw=json.dumps(value).encode();self.send_response(200);self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def do_POST(self):
                observations.append(self.path)
                length=int(self.headers['Content-Length']);self.server.captured=json.loads(self.rfile.read(length))
                if self.headers['Authorization'] != 'Bearer '+token: self.send_error(403);return
                raw=json.dumps({'error':{'message':'bounded non-forwarding fixture','type':'invalid_request_error'}}).encode()
                self.send_response(400);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
            def log_message(self,*args): pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        try:
            with tempfile.TemporaryDirectory() as temporary:
                base=pathlib.Path(temporary).resolve()
                token_file,attestation_file=base/'token',base/'attestation'
                for path,value in [(token_file,token),(attestation_file,attestation)]:
                    path.write_text(value);path.chmod(0o600)
                binary=subprocess.run(['mise','-C',str(ROOT/'components/maka'),'which','maka'],capture_output=True,text=True,check=True).stdout.strip()
                node=subprocess.run(['mise','-C',str(ROOT/'components/maka'),'which','node'],capture_output=True,text=True,check=True).stdout.strip()
                default=base/'untouched-default';default.mkdir();(default/'marker').write_text('preserve')
                with support.owned_normal_host(node=pathlib.Path(node),package_root=pathlib.Path(binary).parent.parent/'maka-agent',relay_helper=ROOT/'components/maka/interactive/configure_maka_openai_relay.mjs',token_file=token_file,attestation_file=attestation_file,default_root=default,evidence_dir=base/'evidence',port=server.server_port) as host:
                    self.assertFalse(host.attestation['processSealed'])
                    self.assertFalse(host.attestation['routingCertification'])
                    pid=host.attestation['pid'];os.kill(pid,0)
                    self.assertTrue(hasattr(support,'run_normal_probe'), 'portable native normal probe runner is missing')
                    prompt='Native normal fixture task. No tools needed.'
                    completed=support.run_normal_probe(host,node=pathlib.Path(node),package_root=pathlib.Path(binary).parent.parent/'maka-agent',installed_helper=ROOT/'components/maka/interactive/maka_skill_routing.mjs',cwd=base,session_id='native-fixture-'+secrets.token_hex(8),prompt=prompt,output_dir=base/'evidence/probe',create=True,model='gpt-5.6-sol',timeout=20)
                    self.assertNotEqual(completed.returncode,0)  # Capture server deliberately stops the turn.
                    users=[item for item in server.captured['input'] if item.get('role')=='user']
                    self.assertEqual(users[-2]['content'][0]['text'],prompt)
                    self.assertTrue(users[-1]['content'][0]['text'].startswith('Runtime Host environment for this turn:'))
                    self.assertTrue((base/'evidence/probe/native-turn-admissions.json').is_file())
                with self.assertRaises(ProcessLookupError): os.kill(pid,0)
                self.assertEqual((default/'marker').read_text(),'preserve')
                self.assertEqual(list(default.iterdir()),[default/'marker'])
                settlement=json.loads((base/'evidence/host-settlement.json').read_text())
                self.assertEqual(settlement['activeSessions'],0)
                self.assertEqual(settlement['pid'],pid)
                self.assertEqual(settlement['rootId'],host.root_id)
                self.assertEqual(settlement['hostEpoch'],host.attestation['hostEpoch'])
                self.assertEqual(observations,['/healthz','/v1/models','/v1/responses'])
        finally:
            server.shutdown();server.server_close();worker.join()

    def test_environment_looking_real_latest_user_cannot_reuse_old_nonce(self):
        spec=importlib.util.spec_from_file_location('maka_relay_native_test',ROOT/'components/maka/interactive/maka_openai_relay.py')
        relay=importlib.util.module_from_spec(spec);sys.modules[spec.name]=relay;spec.loader.exec_module(relay)
        marker='maka:probe-'+'a'*32+'-'+'b'*32
        old='earlier task\nReceipt nonce: '+marker
        def user(text): return {'role':'user','content':[{'type':'input_text','text':text}]}
        latest='Runtime Host environment for this turn:\nThis is a real unmarked user task.'
        self.assertIsNone(relay.routing_request_evidence({'input':[user(old),user(latest)]}))
        latest+='\nReceipt nonce: maka:probe-'+'c'*32+'-'+'d'*32
        evidence=relay.routing_request_evidence({'input':[user(old),user(latest)]})
        self.assertEqual(evidence,(latest.split('Receipt nonce: ')[1],hashlib.sha256(latest.encode()).hexdigest()))

    def test_normal_probes_reject_inherited_routing_seals_and_preloads(self):
        support=self.module()
        for name in ('NODE_OPTIONS','DYLD_INSERT_LIBRARIES','LD_PRELOAD','KHENRIX_MAKA_PRIVATE_HOST_NONCE'):
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError,'preload'):
                support.normal_environment({name:'fixture'})

    def test_changed_native_api_rejected_before_host_or_secret_access(self):
        support=self.module()
        with tempfile.TemporaryDirectory() as directory:
            base=pathlib.Path(directory)
            binary=subprocess.run(['mise','-C',str(ROOT/'components/maka'),'which','maka'],capture_output=True,text=True,check=True).stdout.strip()
            source=pathlib.Path(binary).parent.parent/'maka-agent'
            shutil.copy2(source/'package.json',base/'package.json')
            manifest=json.loads((SUPPORT.parent/'maka_native_api.json').read_text())
            for relative in manifest['files']:
                destination=base/relative;destination.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source/relative,destination)
            changed=base/'node_modules/@maka/runtime-host/dist/client/connect-or-spawn.js'
            changed.write_bytes(changed.read_bytes()+b'\n// internal API changed\n')
            with self.assertRaisesRegex(RuntimeError,'native package'):
                with support.owned_normal_host(node=pathlib.Path('/missing/node'),package_root=base,relay_helper=base/'relay',token_file=base/'secret',attestation_file=base/'attestation',default_root=base/'default',evidence_dir=base/'evidence'):
                    self.fail('unqualified package was launched')
            self.assertFalse((base/'evidence').exists())


if __name__=='__main__': unittest.main()
