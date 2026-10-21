#!/usr/bin/env python3
"""The pinned package overlay is source-guarded and leaves old models intact."""

from __future__ import annotations

import pathlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import apply_gpt6_compat as compat
import install_component as component


class PinnedOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if package_root := os.environ.get("KHENRIX_MAKA_PACKAGE_ROOT"):
            cls.package = pathlib.Path(package_root)
            return
        source = pathlib.Path(__file__).resolve().parent.parent
        layout = component.canonical_layout()
        mise = component.resolve_mise(layout.home)
        account = component.pwd.getpwuid(component.os.getuid())
        environment = component.clean_environment(layout, mise, account.pw_name)
        cls.package = component.discover_package_root(mise, source, environment)

    def copied_sources(self, destination: pathlib.Path) -> None:
        shutil.copy2(self.package / "package.json", destination / "package.json")
        for relative in compat.PATCHES:
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.package / relative, target)

    def test_exact_pinned_sources_gain_gpt6_without_losing_gpt56(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = pathlib.Path(directory)
            self.copied_sources(package)
            receipt = compat.apply_overlay(package)
            self.assertEqual(set(receipt), set(compat.PATCHES))
            for relative, hashes in receipt.items():
                self.assertEqual(
                    compat.sha256((package / relative).read_bytes()), hashes["patched"]
                )
                self.assertNotEqual(hashes["source"], hashes["patched"])
            metadata = (package / "node_modules/@maka/core/dist/model-metadata.js").read_text()
            registry = (package / "node_modules/@maka/core/dist/provider-registry.js").read_text()
            self.assertIn("'gpt-6-sol'", metadata)
            self.assertIn("'gpt-6.1-sol'", metadata)
            self.assertIn("'gpt-5.6-sol'", metadata)
            self.assertIn("'gpt-6-sol'", registry)
            self.assertIn("'gpt-5.6-sol'", registry)

    def test_any_mismatched_source_blocks_every_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = pathlib.Path(directory)
            self.copied_sources(package)
            paths = [package / relative for relative in compat.PATCHES]
            originals = [path.read_bytes() for path in paths]
            paths[-1].write_bytes(originals[-1] + b"\n// changed\n")
            with self.assertRaisesRegex(compat.OverlayError, "source hash differs"):
                compat.apply_overlay(package)
            self.assertEqual([path.read_bytes() for path in paths[:-1]], originals[:-1])

    def test_native_turn_source_and_patched_guards_precede_every_write(self) -> None:
        relative = "node_modules/@maka/runtime/dist/ai-sdk-turn.js"
        self.assertIn(relative, compat.PATCHES)
        for fault in ("source", "patched"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                package = pathlib.Path(directory)
                self.copied_sources(package)
                if fault == "source":
                    path = package / relative
                    path.write_bytes(path.read_bytes() + b"\n// changed\n")
                originals = {name: (package / name).read_bytes() for name in compat.PATCHES}
                if fault == "patched":
                    with patch.dict(compat.PATCHED_HASHES, {relative: "0" * 64}):
                        with self.assertRaisesRegex(compat.OverlayError, "patched hash differs"):
                            compat.apply_overlay(package)
                else:
                    with self.assertRaisesRegex(compat.OverlayError, "source hash differs"):
                        compat.apply_overlay(package)
                self.assertEqual(originals, {name: (package / name).read_bytes() for name in compat.PATCHES})

    def test_pruning_source_and_patched_guards_precede_every_write(self) -> None:
        relative = "node_modules/@maka/runtime/dist/ai-sdk-compaction.js"
        for fault in ("source", "patched"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                package = pathlib.Path(directory)
                self.copied_sources(package)
                if fault == "source":
                    path = package / relative
                    path.write_bytes(path.read_bytes() + b"\n// changed\n")
                originals = {name: (package / name).read_bytes() for name in compat.PATCHES}
                with patch.dict(compat.PATCHED_HASHES,
                                {relative: "0" * 64} if fault == "patched" else {}):
                    with self.assertRaisesRegex(compat.OverlayError, f"{fault} hash differs"):
                        compat.apply_overlay(package)
                self.assertEqual(originals, {name: (package / name).read_bytes() for name in compat.PATCHES})

    def test_candidate_verifier_uses_native_ai_resolution_inside_the_package(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable")
        verifier = pathlib.Path(__file__).with_name("verify_gpt6_candidate.mjs")
        with tempfile.TemporaryDirectory() as directory:
            candidate = pathlib.Path(directory) / "candidate"
            package = candidate / "runtime/package"
            shutil.copytree(self.package, package)
            compat.apply_overlay(package)
            hoisted = package / "node_modules/ai"
            nested = package / "node_modules/@maka/runtime/node_modules/ai"
            self.assertTrue(hoisted.is_dir())
            self.assertFalse(nested.exists())
            for layout in ("hoisted", "nested"):
                with self.subTest(layout=layout):
                    if layout == "nested":
                        nested.parent.mkdir(parents=True, exist_ok=True)
                        hoisted.rename(nested)
                    completed = subprocess.run(
                        [node, str(verifier), str(candidate)],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    self.assertEqual(json.loads(completed.stdout), {
                        "schema": "khenrix-maka-gpt6-compat-v1", "ok": True,
                        "api": "gpt-6.1-sol", "subscription": "gpt-6.1-sol",
                        "resumable": "gpt-5.6-sol",
                    })
            outside = pathlib.Path(directory) / "node_modules/ai"
            outside.parent.mkdir()
            nested.rename(outside)
            for escape in ("ancestor", "symlink"):
                with self.subTest(escape=escape):
                    if escape == "symlink":
                        hoisted.symlink_to(outside, target_is_directory=True)
                    completed = subprocess.run(
                        [node, str(verifier), str(candidate)],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("AI SDK resolves outside the candidate package", completed.stderr)

    def test_routing_keeps_complete_results_while_other_profiles_keep_native_pruning(self) -> None:
        # Removing the routing-only pruning guard must reproduce the archived
        # partial result. Only archive/ledger I/O is replaced with memory storage.
        program = r"""
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
const moduleAt = (root, name) => import(pathToFileURL(root + '/node_modules/@maka/runtime/dist/' + name));
const {AiSdkCompaction: Original} = await moduleAt(process.argv[1], 'ai-sdk-compaction.js');
const {AiSdkCompaction: Patched} = await moduleAt(process.argv[2], 'ai-sdk-compaction.js');
const {buildDefaultContextBudgetPolicy} = await moduleAt(process.argv[1], 'context-budget-policy.js');
const {serializedToolResultProjection} = await moduleAt(process.argv[1], 'tool-result-archive-transition.js');
const value = {ok:true,skill:{name:'brainstorming',instructions:'x'.repeat(18120),truncated:false}};
const response = process.env.KHENRIX_MAKA_PRUNING_RESPONSE
    ? JSON.parse(fs.readFileSync(process.env.KHENRIX_MAKA_PRUNING_RESPONSE, 'utf8'))
    : {id:'response',sessionId:'session',turnId:'turn',role:'tool',partial:false,
       modelVisibility:'visible',content:{id:'call',kind:'function_response',name:'Skill',
       result:{kind:'json',value},modelProjection:{version:1,kind:'json',value}}};
const serialized = serializedToolResultProjection(response.content.modelProjection);
assert.ok(Buffer.byteLength(serialized) > 7500);
if (process.env.KHENRIX_MAKA_PRUNING_RESPONSE) {
    assert.equal(Buffer.byteLength(serialized), 18211);
    assert.equal(createHash('sha256').update(serialized).digest('hex'),
        'b394bbd7be789232fdccf383f82d304e6cf54a2d0436db06602c0140384b74bc');
}
async function run(Compaction, profile, events=[response], initial=[], overrides={}) {
    const transitions = structuredClone(initial), archives = [];
    let reads = 0;
    const input = {header:{toolProfile:profile},contextBudget:buildDefaultContextBudgetPolicy(),
        toolResultArchive:{services:{archiveToolResult:async request => {
            archives.push(request); return {ledger:true};
        }}},recordModelProjectionTransition:async transition => transitions.push(transition),
        loadModelProjectionTransitions:async () => {
            reads++; return {transitions:[...transitions],unreadableTargets:new Set(),unscopedUnreadable:0};
        },...overrides};
    const compaction = new Compaction({input,sessionId:response.sessionId,now:()=>123});
    const before = structuredClone(events), policy = structuredClone(input.contextBudget);
    const first = await compaction.pruneToolResults(events, response.turnId);
    const continuation = await compaction.pruneToolResults(events, response.turnId);
    assert.deepEqual(events, before);
    assert.deepEqual(input.contextBudget, policy);
    return {first,continuation,transitions,archives,reads};
}
const native = await run(Original, 'khenrix-skill-routing-v1');
assert.equal(native.archives.length, 1);
const envelope = native.first.events[0].content.modelProjection.value;
assert.equal(envelope.kind, 'maka.archived_tool_result');
assert.equal(envelope.reason, 'tool_result_pruned');
assert.equal(envelope.originalBytes, Buffer.byteLength(serialized));
assert.ok(Buffer.byteLength(JSON.stringify(envelope)) <= 7500);
assert.ok(envelope.page.next);
assert.ok(envelope.page.content.length < serialized.length);
assert.ok(serialized.startsWith(envelope.page.content));
const routed = await run(Patched, 'khenrix-skill-routing-v1');
const digest = value => createHash('sha256').update(value).digest('hex');
assert.equal(digest(serializedToolResultProjection(routed.first.events[0].content.modelProjection)), digest(serialized),
    'routing must retain the complete Skill result before provider continuation');
assert.deepEqual(routed.continuation.events, [response]);
assert.equal(routed.archives.length, 0);
assert.equal(routed.transitions.length, 0);
assert.equal(routed.reads, 2);
for (const name of ['Skill', 'SkillSearch']) {
    const event = structuredClone(response);
    event.content.name = name;
    const result = await run(Patched, 'khenrix-skill-routing-v1', [event]);
    assert.deepEqual(result.first.events, [event]);
    assert.deepEqual(result.continuation.events, [event]);
    assert.equal(result.archives.length, 0);
}
for (const profile of [undefined, 'default', 'workhub-coordination-v1', 'workhub-coordination-v2',
                       'khenrix-skill-routing-v1-other']) {
    assert.deepEqual(await run(Patched, profile), await run(Original, profile));
}
// Existing transitions remain authoritative even in routing sessions; the
// exemption may prevent new lossy writes but must never resurrect old content.
for (const profile of ['khenrix-skill-routing-v1', undefined]) {
    assert.deepEqual(await run(Patched, profile, [response], native.transitions),
        await run(Original, profile, [response], native.transitions));
    const unreadable = {loadModelProjectionTransitions:async () => ({
        transitions:[],unreadableTargets:new Set(),unscopedUnreadable:1})};
    await assert.rejects(run(Patched, profile, [response], [], unreadable),
        /unscoped unreadable record/);
    for (const overrides of [{contextBudget:undefined}, {toolResultArchive:undefined},
                             {recordModelProjectionTransition:undefined}]) {
        assert.deepEqual(await run(Patched, profile, [response], [], overrides),
            await run(Original, profile, [response], [], overrides));
    }
}
console.log(JSON.stringify({ok:true,originalBytes:Buffer.byteLength(serialized),
    nativeArchivedBytes:Buffer.byteLength(JSON.stringify(envelope)),
    routingArchivedResults:routed.archives.length,ordinaryProfilesCompared:5}));
"""
        with tempfile.TemporaryDirectory() as directory:
            package = pathlib.Path(directory) / "package"
            shutil.copytree(self.package, package)
            compat.apply_overlay(package)
            completed = subprocess.run(
                [shutil.which("node"), "--input-type=module", "-e", program,
                 str(self.package), str(package)],
                capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            print(completed.stdout.strip())

    def test_routing_context_dispatch_preserves_original_identity_and_order(self) -> None:
        relative = "node_modules/@maka/runtime/dist/ai-sdk-turn.js"
        self.assertIn(relative, compat.PATCHES)
        source = compat.patched_content(relative, (self.package / relative).read_bytes()).decode()
        # Execute the actual replacement in the installed source, without an SDK
        # reimplementation. Native Host wire composition has a separate fixture.
        start = source.index("                        const dispatchMessages =")
        end = source.index("                        this.memorySourceMessages", start)
        block = source[start:end]
        program = r"""
const assert = require('node:assert/strict');
const dispatch = new Function('attemptMessages', 'dynamicContextMessages', BLOCK + '\nreturn dispatchMessages;');
const routing = {deps:{backend:{header:{toolProfile:'khenrix-skill-routing-v1'}}}};
const normal = {deps:{backend:{header:{toolProfile:'workhub-coordination-v2'}}}};
const old = {role:'user',content:'old nonce'};
const assistant = {role:'assistant',content:[{type:'tool-call',toolCallId:'a'},{type:'tool-call',toolCallId:'b'}]};
const tools = {role:'tool',content:[{type:'tool-result',toolCallId:'a'},{type:'tool-result',toolCallId:'b'}]};
const latest = {role:'user',content:'Runtime Host environment for this turn: real user request'};
const contexts = [{role:'user',content:'context 1'},{role:'system',content:'context 2'}];
const original = [old,assistant,tools,latest];
Object.freeze(original);Object.freeze(contexts);original.forEach(Object.freeze);contexts.forEach(Object.freeze);
const actual = dispatch.call(routing,original,contexts);
assert.deepEqual(actual,[old,assistant,tools,...contexts,latest]);
actual.forEach((value,index)=>assert.equal(value,[old,assistant,tools,...contexts,latest][index]));
assert.deepEqual(dispatch.call(normal,original,contexts),[...original,...contexts]);
assert.deepEqual(dispatch.call(routing,[assistant,tools],contexts),[...contexts,assistant,tools]);
assert.deepEqual(dispatch.call(routing,original,[]),original);
assert.deepEqual(dispatch.call(routing,[],contexts),contexts);
// Each physical attempt uses the recovered history; continuation/full-history
// fallback can change history without caching the previous insertion position.
for (const messages of [[latest],[old,latest],[old,assistant,tools,latest],[assistant,tools]]) {
 const result=dispatch.call(routing,messages,contexts);
 const last=messages.findLastIndex(message=>message.role==='user');
 assert.deepEqual(result,last<0?[...contexts,...messages]:[...messages.slice(0,last),...contexts,...messages.slice(last)]);
}
""".replace('BLOCK', json.dumps(block))
        completed = subprocess.run([shutil.which("node"), "-e", program], capture_output=True, text=True, timeout=30)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertLess(source.index("for (;;) {", source.index("let attemptMessages = projectedMessages")), start)
        self.assertIn("attemptMessages = recovered.messages", source[end:])

    def test_skill_routing_hides_archive_read_without_disabling_compaction(self) -> None:
        relative = "node_modules/@maka/runtime/dist/ai-sdk-backend.js"
        self.assertIn(relative, compat.PATCHES)
        patched = compat.patched_content(relative, (self.package / relative).read_bytes())
        source = patched.decode("utf-8")
        self.assertIn("this.input.header.toolProfile === 'khenrix-skill-routing-v1'", source)
        self.assertIn("? undefined : this.input.toolResultArchive", source)
        self.assertIn("this.compaction = new AiSdkCompaction({\n            input,", source)
        self.assertIn("bindToolResultArchiveDecoder", source)

    def test_current_transport_capture_uses_only_its_native_package_sdk(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("Node is unavailable")
        capture = pathlib.Path(__file__).resolve().parents[1] / "controller/provider-request-contract.mjs"
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            package = root / "package"
            shutil.copytree(self.package, package)
            output = root / "request.json"
            hoisted = package / "node_modules/ai"
            nested = package / "node_modules/@maka/runtime/node_modules/ai"
            for layout in ("hoisted", "nested"):
                with self.subTest(layout=layout):
                    if layout == "nested":
                        nested.parent.mkdir(parents=True, exist_ok=True)
                        hoisted.rename(nested)
                    completed = subprocess.run(
                        [node, str(capture), str(package), str(output)],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertEqual(completed.returncode, 0, completed.stderr)
                    observed = json.loads(output.read_text())
                    self.assertEqual(observed["websocket"]["method"], "GET")
                    self.assertEqual(observed["websocket"]["path"], "/v1/responses")
                    self.assertEqual(len(observed["fallbackBodies"]), 2)
                    self.assertTrue(all(body["model"] == "gpt-5.6-sol"
                                        for body in observed["fallbackBodies"]))
            outside = root / "node_modules/ai"
            outside.parent.mkdir()
            nested.rename(outside)
            for escape in ("ancestor", "symlink"):
                with self.subTest(escape=escape):
                    if escape == "symlink":
                        hoisted.symlink_to(outside, target_is_directory=True)
                    completed = subprocess.run(
                        [node, str(capture), str(package)],
                        capture_output=True, text=True, timeout=30,
                    )
                    self.assertNotEqual(completed.returncode, 0)
                    self.assertIn("AI SDK resolves outside the candidate package", completed.stderr)

    def test_skill_tool_loads_complete_superpowers_instructions(self) -> None:
        relative = "node_modules/@maka/runtime/dist/skills-metadata.js"
        self.assertIn(relative, compat.PATCHES)
        patched = compat.patched_content(relative, (self.package / relative).read_bytes())
        self.assertIn("MAX_SKILL_TOOL_BODY_CHARS = 40_000", patched.decode("utf-8"))

    def test_component_only_layout_runs_overlay_tests_without_repository_skills(self) -> None:
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            component_root = pathlib.Path(directory) / "maka"
            shutil.copytree(pathlib.Path(__file__).resolve().parents[1], component_root)
            self.assertFalse((component_root.parent / "shared").exists())
            completed = subprocess.run(
                [sys.executable, "-c",
                 "import pathlib,sys,unittest; import test_maka_gpt6_compat as tests; "
                 "tests.PinnedOverlayTests.setUpClass = classmethod("
                 "lambda cls: setattr(cls, 'package', pathlib.Path(sys.argv[1]))); "
                 "unittest.main(module=tests, argv=['unittest', "
                 "'PinnedOverlayTests.test_skill_tool_loads_complete_superpowers_instructions'])",
                 str(self.package)],
                cwd=component_root / "scripts", capture_output=True, text=True,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)


if __name__ == "__main__":
    unittest.main()
