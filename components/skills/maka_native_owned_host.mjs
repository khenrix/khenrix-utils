import {readFile, access} from 'node:fs/promises';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
import {randomUUID, createHash} from 'node:crypto';
const spec = JSON.parse(await readFile(process.argv[2], 'utf8'));
if (Object.keys(process.env).some(name => name.startsWith('NODE_') || name.startsWith('DYLD_') || name.startsWith('KHENRIX_MAKA_PRIVATE_HOST_') || name === 'LD_PRELOAD')) throw new Error('Stock Host must not inherit routing seals or preload controls');
// Validate licensed internal APIs before importing or accessing caller credentials.
const manifest = JSON.parse(await readFile(new URL('./maka_native_api.json', import.meta.url), 'utf8'));
const descriptor = JSON.parse(await readFile(join(spec.packageRoot, 'package.json'), 'utf8'));
if (descriptor.name !== manifest.package || descriptor.version !== manifest.version) throw new Error('Native package identity differs');
for (const [relative, expected] of Object.entries(manifest.files)) {
 const actual = createHash('sha256').update(await readFile(join(spec.packageRoot, relative))).digest('hex');
 if (actual !== expected) throw new Error('Native internal API hash differs: ' + relative);
}
const load = relative => import(pathToFileURL(join(spec.packageRoot, relative)));
const {resolveStorageRoot, resolveRootControlNamespace} = await load('node_modules/@maka/storage/dist/root-authority.js');
const {connectOwnedRuntimeHost} = await load('node_modules/@maka/runtime-host/dist/client/connect-or-spawn.js');
const {connectExistingRuntimeHost} = await load('node_modules/@maka/runtime-host/dist/client/connection.js');
const protocol = await load('node_modules/@maka/runtime-host/dist/protocol/index.js');
const catalog = await load('node_modules/@maka/runtime-host/dist/client/catalog-reader.js');
const registry = await load('node_modules/@maka/core/dist/provider-registry.js');
const relay = await import(pathToFileURL(spec.relayHelper));
const range = {min: protocol.RUNTIME_HOST_PROTOCOL_VERSION, max: protocol.RUNTIME_HOST_PROTOCOL_VERSION};
async function snapshotDefault() {
 let found;
 try {found = await connectExistingRuntimeHost({rootPath: spec.defaultRootPath, clientInstanceId: randomUUID(), protocol: range, compositionId: protocol.INTERACTIVE_RUNTIME_HOST_COMPOSITION_ID});}
 catch(error){if(error.code === 'root_unmarked')return {kind:'unmarked'};throw error;}
 if (found.kind === 'unavailable') return {kind: 'unavailable'};
 if (found.kind !== 'connected') throw new Error('Default Host snapshot unavailable');
 try {
  const diagnostics = await found.connection.request('host.diagnostics.query', {});
  const sessions = [];let query={kind:'list_start'};
  for(let index=0;index<64;index++) {
   const page = await found.connection.request('session.catalog.query',query);
   sessions.push(...page.sessions.map(session=>session.id));
   if(page.nextCursor === null) return {rootId:found.connection.rootId,pid:diagnostics.pid,hostEpoch:diagnostics.hostEpoch,sessions:sessions.sort()};
   query={kind:'list_continue',revision:page.revision,cursor:page.nextCursor};
  }
  throw new Error('Default Host session list exceeds evidence bound');
 } finally {await found.connection.close();}
}
const before = await snapshotDefault();
if(spec.rootPath === spec.defaultRootPath)throw new Error('Default root cannot be owned by a probe');
const authority = await resolveStorageRoot({path: spec.rootPath, kind:'interactive'});
const control = join(resolveRootControlNamespace(),authority.rootId);
let controlExists=false;try {await access(control);controlExists=true;}catch(error){if(error.code!=='ENOENT')throw error;}
if(controlExists || spec.rootPath === spec.defaultRootPath || authority.canonicalPath !== spec.rootPath)throw new Error('Stock owned root must be fresh and separate');
const owned = await connectOwnedRuntimeHost({rootPath:spec.rootPath,clientInstanceId:randomUUID(),protocol:range,compositionId:protocol.INTERACTIVE_RUNTIME_HOST_COMPOSITION_ID,initialization:{incognito:true},closeOnLauncherExit:true});
if(owned.kind!=='connected')throw new Error('Stock owned Host unavailable: '+owned.reason);
let readiness;
try {
 const {connection,host}=owned;
 const diagnostics=await connection.request('host.diagnostics.query',{});
 if(connection.rootId!==authority.rootId || diagnostics.pid!==host.pid || !diagnostics.hostEpoch)throw new Error('Stock Host PID/root/epoch mismatch');
 // Existing native reconciler writes only this fresh private Host's connection catalog/vault.
 const token=await relay.readPrivateToken(spec.tokenFile);
 const attestation=await relay.readPrivateToken(spec.attestationFile);
 await relay.reconcileMakaRelay(connection,{token,port:spec.port,readCatalog:catalog.readRuntimeHostConnectionCatalog,providerAuthKind:type=>registry.PROVIDER_REGISTRY[type]?.authKind,assertReady:({token:callerToken,port})=>relay.assertAttestedRelayReady({token:callerToken,attestation,port})});
 if(JSON.stringify(await snapshotDefault())!==JSON.stringify(before))throw new Error('Default Host changed during stock setup');
 readiness={schema:'maka-stock-normal-owned-host-v1',rootId:connection.rootId,rootPath:spec.rootPath,clientDataRoot:spec.clientDataRoot,pid:host.pid,hostEpoch:diagnostics.hostEpoch,nodePath:process.execPath,platform:process.platform,processSealed:false,routingCertification:false,defaultHost:before};
 process.stdout.write(JSON.stringify(readiness)+'\n');
 await new Promise(resolve=>{process.stdin.once('end',resolve);process.stdin.resume();});
} finally {
 // Recheck the exact owned identity, then wait for all native turns to settle.
 const current = await owned.connection.request('host.diagnostics.query', {});
 if (current.pid !== owned.host.pid || owned.connection.rootId !== authority.rootId
     || (readiness && current.hostEpoch !== readiness.hostEpoch)) throw new Error('Owned Host identity changed before settlement');
 const deadline = Date.now() + 20_000;
 for (;;) {
  let query={kind:'list_start'};let active=0;let terminal=false;
  for(let index=0;index<64;index++) {
   const page=await owned.connection.request('session.catalog.query',query);
   active+=page.sessions.filter(session=>session.liveRunState?.runningTurnIds?.length).length;
   if(page.nextCursor===null){terminal=true;break;}
   query={kind:'list_continue',revision:page.revision,cursor:page.nextCursor};
  }
  if(!terminal)throw new Error('Owned Host catalog exceeded settlement bound');
  if(active===0)break;
  if(Date.now()>=deadline)throw new Error('Owned Host still has running turns; exact child retained');
  await new Promise(resolve=>setTimeout(resolve,100));
 }
 await owned.connection.close();
 const settled=await owned.host.settle(5000);
 if(!settled)throw new Error('Owned stock Host did not settle cleanly');
 const after=await snapshotDefault();
 if(JSON.stringify(after)!==JSON.stringify(before))throw new Error('Default Host changed during stock observation');
 if(readiness)process.stdout.write(JSON.stringify({schema:'maka-stock-normal-owned-host-settled-v1',rootId:readiness.rootId,pid:readiness.pid,hostEpoch:readiness.hostEpoch,activeSessions:0,defaultHost:after})+'\n');
}
