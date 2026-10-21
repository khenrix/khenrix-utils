"""Strict evidence-only native Read continuation verification."""
import base64
import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

def units(text):
    return len(text.encode("utf-16-le")) // 2

def complete_reference_chains(reads, sources, workspace_root):
    ids = [row.get("call", {}).get("id") for row in reads]
    if len(ids) != len(set(ids)) or any(not value for value in ids):
        raise RuntimeError("Read evidence has missing or duplicate call IDs")
    proofs = []
    for relative, source in sources.items():
        path, expected = source
        expected_text = expected.decode("utf-8")
        expected_hash = hashlib.sha256(expected).hexdigest()
        def resolves(value):
            return isinstance(value, str) and "://" not in value and (workspace_root / value).resolve() == path.resolve()
        starts = [(index, row) for index, row in enumerate(reads) if resolves(row.get("call", {}).get("args", {}).get("path"))]
        if len(starts) != 1:
            raise RuntimeError(relative + ": missing or duplicate initial native Read")
        index, row = starts[0]
        if set(row["call"]["args"]) - {"path", "offset"} or row["call"]["args"].get("offset", 0) != 0:
            raise RuntimeError(relative + ": initial Read range is incomplete")
        text, chain = "", []
        used = set()
        while True:
            call, response = row["call"], row["response"]
            if (call.get("id") in used or response.get("id") != call.get("id")
                    or call.get("name") != "Read" or response.get("name") != "Read"
                    or response.get("isError") or response.get("modelProjection", {}).get("isError")):
                raise RuntimeError(relative + ": native call/result join failed")
            used.add(call["id"])
            value = response.get("result", {}).get("value")
            if not isinstance(value, dict) or not isinstance(value.get("content"), str):
                raise RuntimeError(relative + ": native Read returned no text page")
            if (type(value.get("offset")) is not int or type(value.get("returnedLines")) is not int
                    or type(value.get("totalLines")) is not int or value["offset"] != text.count("\n")
                    or value["totalLines"] != expected_text.count("\n") + 1):
                raise RuntimeError(relative + ": native line coverage is inconsistent")
            body = value["content"]
            next_value = value.get("next", "missing")
            text += body
            if not expected_text.startswith(text):
                raise RuntimeError(relative + ": native page differs from source coverage")
            chain.append({"call_id": call["id"], "path": call["args"]["path"],
                          "page_sha256": hashlib.sha256(body.encode()).hexdigest(),
                          "offset": value["offset"], "returnedLines": value["returnedLines"],
                          "totalLines": value["totalLines"], "next": next_value})
            if next_value is None:
                if value["returnedLines"] != body.count("\n") + 1:
                    raise RuntimeError(relative + ": returned line count is inconsistent")
                break
            if not isinstance(next_value, dict) or set(next_value) != {"path"}:
                raise RuntimeError(relative + ": unsupported or incomplete native continuation object")
            uri = urlsplit(next_value["path"])
            query = parse_qs(uri.query, strict_parsing=True)
            try:
                encoded = uri.path.lstrip("/")
                origin = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
                raw_position = query["at"][0]
                if re.fullmatch(r"[0-9]+", raw_position) is None:
                    raise ValueError("non-native UTF16 position")
                position = int(raw_position)
                if position > 2**53 - 1:
                    raise ValueError("unsafe UTF16 position")
            except (ValueError, KeyError, UnicodeError) as error:
                raise RuntimeError(relative + ": invalid continuation URI") from error
            if (uri.scheme != "maka" or uri.netloc != "read" or uri.fragment
                    or set(query) != {"at", "sha"} or any(len(values) != 1 for values in query.values())
                    or not resolves(origin) or query["sha"][0] != expected_hash[:32]):
                raise RuntimeError(relative + ": continuation path or content hash differs")
            # partialLine also marks a page that STARTS inside a line. Only
            # source coverage and the UTF16 cursor identify an omitted end LF.
            boundary_lf = expected_text[len(text):len(text) + 1] == "\n"
            if (len(text) >= len(expected_text)
                    or position != units(text) + int(boundary_lf)):
                raise RuntimeError(relative + ": native continuation has a gap or overlap")
            if value["returnedLines"] != body.count("\n") + int(boundary_lf):
                raise RuntimeError(relative + ": returned line count is inconsistent")
            if boundary_lf:
                text += "\n"
            candidates = [(following, item) for following, item in enumerate(reads)
                          if item.get("call", {}).get("args") == next_value]
            if len(candidates) != 1 or candidates[0][0] <= index:
                raise RuntimeError(relative + ": missing, duplicated, or out-of-order continuation")
            index, row = candidates[0]
        raw = text.encode()
        if raw != expected:
            raise RuntimeError(relative + ": terminal native pages do not equal the complete installed bytes")
        proofs.append({"reference": relative, "full_installed_reference_match": relative,
                       "sha256": expected_hash, "byte_length": len(raw), "pages": chain,
                       "reconstructed_bytes": raw})
    return proofs
