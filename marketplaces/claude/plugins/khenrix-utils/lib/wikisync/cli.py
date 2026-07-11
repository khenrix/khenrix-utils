"""JSON job-protocol CLI — the deterministic spine both skills drive.

The SKILL.md bodies never call each other as skills (Markdown isn't a portable callable
across CLIs); they shell out to `python3 -m wikisync <cmd>` with JSON in/out. The LLM
edges (fetch, extract, classify) produce an extraction JSON; this CLI validates it,
renders deterministically, writes under the vault lock, and records state.

Commands:
  probe                      capability matrix (feature-detect chrome-devtools/watch/vault)
  plan --channel C           snapshot → diff → prepared job list (unavailable → deferred)
  commit --job JSON          validate extraction → render → write page → record + captures
  reprocess [--filter]       re-render from cached extraction (no network); manual survives
  refetch  / reclassify      list candidates whose fetcher/taxonomy version < target
  adopt --path MD            register an existing vault page by its source_url (no dup)
  report                     job-state counts for the run
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .canonurl import canonicalize
from .capture import CaptureStore
from .config import Config, load_config
from .ledger import Ledger
from .render import render_page
from .sources import SourceItem
from .sources.bookmarks import read_bookmarks
from .sources.instagram_export import read_export
from .taxonomy import TAXONOMY_VERSION, route

# extraction keys that are provenance/control, not page content — excluded from the
# stored "extraction" capture's identity but kept for re-render.
_STANDARD_CAPS = ["caption", "comments", "metadata"]


@dataclass
class Context:
    cfg: Config
    ledger: Ledger
    store: CaptureStore


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
def cmd_probe(ctx: Context) -> dict:
    cfg = ctx.cfg
    export_ok = bool(cfg.instagram_export_dir) and Path(cfg.instagram_export_dir).exists()
    return {
        "bookmarks": Path(cfg.chrome_profile).is_file(),
        "instagram_export": export_ok,
        # the Python process can't see Claude's MCP; the SKILL sets this env when the
        # chrome-devtools tool is present, else it stays off (never assumed).
        "instagram_live": os.environ.get("WIKISYNC_CHROME_DEVTOOLS") == "1"
                          or bool(cfg.instagram_live_optin),
        "watch": shutil.which("yt-dlp") is not None,
        "wiki_plugin": (Path(cfg.vault) / "wiki").is_dir(),
    }


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def _build_snapshot(ctx: Context, channel: str, snapshot_file: str | None):
    if snapshot_file:                       # live: SKILL wrote a normalized snapshot
        data = json.loads(Path(snapshot_file).read_text())
        from .sources import Snapshot
        return Snapshot(channel=data.get("channel", channel),
                        scope=data.get("scope", "all"),
                        status=data.get("status", "partial"),
                        items=[SourceItem(**it) for it in data.get("items", [])])
    if channel == "chrome-bookmarks":
        return read_bookmarks(ctx.cfg.chrome_profile)
    if channel == "instagram-export":
        export = Path(ctx.cfg.instagram_export_dir or "") / "saved_posts.json"
        return read_export(export)
    return None                              # instagram-live w/o snapshot, unknown → deferred


def _target_capabilities(url: str) -> list[str]:
    kind = canonicalize(url).kind
    caps = list(_STANDARD_CAPS)
    if kind in ("instagram_reel", "youtube"):
        caps.append("transcript-if-deep")
    return caps


def cmd_plan(ctx: Context, channel: str, snapshot_file: str | None = None,
             now: str = "") -> dict:
    snap = _build_snapshot(ctx, channel, snapshot_file)
    if snap is None or snap.status == "unavailable":
        return {"channel": channel, "deferred": True,
                "reason": "capability_unavailable", "jobs": [], "removable": []}
    if snap.status == "failed":
        return {"channel": channel, "deferred": True, "reason": "enumeration_failed",
                "jobs": [], "removable": [], "errors": snap.errors}

    diff = ctx.ledger.plan_diff(snap)
    jobs = []
    for reason, group in (("new", diff.new), ("reappeared", diff.reappeared),
                          ("updated", diff.updated)):
        for it in group:
            iid = ctx.ledger.observe(chan=snap.channel, native_id=it.native_id,
                                     url=it.canonical_url, collection=it.collection,
                                     title=it.title, now=now)
            ctx.ledger.job_transition(iid, "prepared")
            jobs.append({
                "item_id": iid, "native_id": it.native_id,
                "canonical_url": it.canonical_url, "collection": it.collection,
                "title": it.title, "source_channel": snap.channel,
                "state": "prepared", "reason": reason,
                "target_capabilities": _target_capabilities(it.canonical_url),
            })
    return {
        "channel": snap.channel, "deferred": False,
        "snapshot_status": snap.status, "jobs": jobs,
        # removable listed but NEVER auto-deleted; the sync skill marks source_removed
        "removable": [{"native_id": r.native_id, "canonical_url": r.canonical_url}
                      for r in diff.removable],
    }


# --------------------------------------------------------------------------- #
# commit
# --------------------------------------------------------------------------- #
def _source_url(job: dict) -> str:
    url = job.get("source_url") or job.get("canonical_url")
    if not url:
        raise ValueError("commit: extraction JSON missing source_url/canonical_url")
    return url


def _write_page(ctx: Context, rel_path: str, text: str) -> None:
    dest = Path(ctx.cfg.vault) / rel_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    lock = Path(ctx.cfg.vault) / "scripts" / "wiki-lock.sh"
    if lock.is_file():                       # honor the vault's per-file lock if present
        try:
            subprocess.run(["bash", str(lock), "acquire", rel_path], cwd=ctx.cfg.vault,
                           check=False, capture_output=True, timeout=30)
            dest.write_text(text)
            return
        finally:
            subprocess.run(["bash", str(lock), "release", rel_path], cwd=ctx.cfg.vault,
                           check=False, capture_output=True, timeout=30)
    dest.write_text(text)


def _extraction_only(job: dict) -> dict:
    """The content-bearing slice of a job, for the reprocess-from-cache capture."""
    return {k: v for k, v in job.items() if k not in ("captures", "now")}


def cmd_commit(ctx: Context, job: dict, now: str | None = None) -> dict:
    now = now if now is not None else job.get("now", "")
    c = canonicalize(_source_url(job))
    native_id = job.get("native_id") or c.native_id or c.canonical
    channel = job.get("source_channel") or "manual"
    title = job.get("title", "")
    collection = job.get("collection", "")

    item = SourceItem(native_id=native_id, canonical_url=c.canonical,
                      title=title, collection=collection)
    iid = ctx.ledger.observe(chan=channel, native_id=native_id, url=c.canonical,
                             collection=collection, title=title, now=now)

    existing_row = ctx.ledger.find_page_by_url(c.canonical)
    existing_text = None
    if existing_row is not None:
        p = Path(ctx.cfg.vault) / existing_row["path"]
        if p.is_file():
            existing_text = p.read_text()

    r = route(item, job)
    doc = render_page(item, job, r, existing_text=existing_text, now=now)

    # persist raw captures + a reprocessable extraction snapshot
    for cap in job.get("captures", []) or []:
        payload = (cap.get("text") or "").encode("utf-8")
        stored = ctx.store.put(native_id, cap.get("kind", "raw"), payload)
        ctx.ledger.record_capture(capture_id=stored.capture_id, item_id=iid,
                                  kind=cap.get("kind", "raw"),
                                  capture_hash=stored.capture_hash,
                                  raw_path=stored.raw_path, now=now)
    ext_blob = json.dumps(_extraction_only(job), sort_keys=True).encode("utf-8")
    ctx.store.put(native_id, "extraction", ext_blob)

    _write_page(ctx, doc.path, doc.text)
    ctx.ledger.record_page(path=doc.path, source_url=c.canonical,
                           generated_hash=doc.generated_hash, now=now)
    ctx.ledger.job_transition(iid, "committed")
    return {"path": doc.path, "generated_hash": doc.generated_hash, "committed": True}


# --------------------------------------------------------------------------- #
# reprocess — re-render from cached extraction, no network
# --------------------------------------------------------------------------- #
def cmd_reprocess(ctx: Context, now: str = "", only_stale: bool = False) -> list[str]:
    out = []
    for prow in ctx.ledger.all_pages():
        url = prow["source_url"]
        items = ctx.ledger.active_items_for_url(url)
        if not items:
            continue
        it = items[0]
        cap = ctx.store.latest(it["native_id"], "extraction")
        if cap is None:
            continue
        ext = json.loads(ctx.store.get(cap.capture_id))
        if only_stale and int(ext.get("taxonomy_version", 0)) >= TAXONOMY_VERSION:
            continue
        item = SourceItem(native_id=it["native_id"], canonical_url=url,
                          title=it["title"] or "", collection=ext.get("collection", ""))
        r = route(item, ext)
        page_path = Path(ctx.cfg.vault) / prow["path"]
        existing = page_path.read_text() if page_path.is_file() else None
        doc = render_page(item, ext, r, existing_text=existing, now=now)
        _write_page(ctx, doc.path, doc.text)
        ctx.ledger.record_page(path=doc.path, source_url=url,
                               generated_hash=doc.generated_hash, now=now)
        out.append(doc.path)
    return out


# --------------------------------------------------------------------------- #
# adopt — register an existing vault page without duplicating it
# --------------------------------------------------------------------------- #
def _parse_frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        if ":" not in line or line.strip().startswith("-"):
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if v and v[0] in "\"'" and v[-1] == v[0]:
            v = v[1:-1]
        out[k.strip()] = v
    return out


def cmd_adopt(ctx: Context, md_path: str, now: str = "") -> dict:
    p = Path(md_path)
    fm = _parse_frontmatter(p.read_text())
    source_url = fm.get("source_url")
    if not source_url:
        raise ValueError(f"adopt: {md_path} has no source_url in frontmatter")
    c = canonicalize(source_url)
    channel = fm.get("source_channel") or "manual"
    native_id = fm.get("native_id") or c.native_id or c.canonical
    ctx.ledger.observe(chan=channel, native_id=native_id, url=c.canonical,
                       title=fm.get("title", ""), now=now)
    try:
        rel = str(p.resolve().relative_to(Path(ctx.cfg.vault).resolve()))
    except ValueError:
        rel = str(p)
    page_id = ctx.ledger.record_page(path=rel, source_url=c.canonical, now=now)
    return {"page_id": page_id, "source_url": c.canonical, "path": rel, "adopted": True}


def cmd_report(ctx: Context) -> dict:
    return ctx.ledger.job_state_counts()


# --------------------------------------------------------------------------- #
# argparse entrypoint
# --------------------------------------------------------------------------- #
def _make_context(args) -> Context:
    cfg = load_config(args.config) if args.config else Config()
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    ledger = Ledger(cfg.ledger_path)
    return Context(cfg=cfg, ledger=ledger, store=CaptureStore(cfg.state_dir))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="wikisync", description="wiki-sync deterministic core")
    ap.add_argument("--config", help="path to config.json (else machine defaults)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    pp = sub.add_parser("plan")
    pp.add_argument("--channel", required=True)
    pp.add_argument("--snapshot-file")
    pp.add_argument("--now", default="")
    cp = sub.add_parser("commit")
    cp.add_argument("--job", required=True, help="JSON string or @path")
    cp.add_argument("--now", default="")
    rp = sub.add_parser("reprocess")
    rp.add_argument("--only-stale", action="store_true")
    rp.add_argument("--now", default="")
    ap_ = sub.add_parser("adopt")
    ap_.add_argument("--path", required=True)
    sub.add_parser("report")

    args = ap.parse_args(argv)
    ctx = _make_context(args)

    if args.cmd == "probe":
        print(json.dumps(cmd_probe(ctx), indent=2))
    elif args.cmd == "plan":
        print(json.dumps(cmd_plan(ctx, args.channel, args.snapshot_file, args.now), indent=2))
    elif args.cmd == "commit":
        raw = args.job
        job = json.loads(Path(raw[1:]).read_text() if raw.startswith("@") else raw)
        print(json.dumps(cmd_commit(ctx, job, args.now), indent=2))
    elif args.cmd == "reprocess":
        print(json.dumps(cmd_reprocess(ctx, args.now, args.only_stale), indent=2))
    elif args.cmd == "adopt":
        print(json.dumps(cmd_adopt(ctx, args.path), indent=2))
    elif args.cmd == "report":
        print(json.dumps(cmd_report(ctx), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
