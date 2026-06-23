---
name: markitdown
description: >-
  Convert documents to Markdown — PDF, DOCX, XLSX, PPTX, HTML, CSV/JSON/XML, images (OCR/EXIF),
  audio (transcription), ZIP archives, and YouTube URLs — by wrapping Microsoft's `markitdown` CLI run
  through `uvx`, so this repo adds NO dependency (nothing installed, nothing in requirements). Use when the
  user wants a file's contents as Markdown / plain text, to extract or read text from a PDF/Word/Excel/
  PowerPoint/webpage/image, to feed a document to an LLM, or to OCR a scanned PDF. Triggers: "convert this
  PDF to markdown", "extract the text from this docx/xlsx/pptx", "turn this document into markdown",
  "read this PDF as text", "markitdown <file>", "get the text out of this image/slide deck", "ocr this
  scanned pdf", "transcribe this audio to markdown", "convert this webpage/youtube link to markdown".
allowed-tools: Bash, Read
---

# markitdown

Convert a document to Markdown with Microsoft's `markitdown`, invoked via `uvx` — **zero install, no repo
dependency**. `uvx` fetches the package into an ephemeral, cached environment and runs it; nothing is added
to this repo, no venv, no `pip install`. Supports PDF, DOCX, XLSX/XLS, PPTX, HTML, CSV/JSON/XML, images
(OCR + EXIF), audio (speech→text), ZIP (recurses members), and YouTube URLs.

## The one rule that matters

**Always invoke with `--from 'markitdown[all]'`. Never bare `uvx markitdown`.**

```bash
uvx --from 'markitdown[all]' markitdown "<INPUT>" -o "<OUTPUT.md>"
```

Bare `uvx markitdown` resolves the **base** package only — it has no PDF/DOCX/XLSX/PPTX extras and **fails on
real office files** (or silently emits empty/garbled output). The `[all]` extra is what pulls in `pdfminer`,
`python-docx`, `openpyxl`, `python-pptx`, etc. This is the single most common mistake; get it right every
time. Quote `'markitdown[all]'` so the shell doesn't glob the brackets.

## Preflight (run once, before converting)

`uvx` ships with `uv`. Check it's present:

```bash
command -v uv
```

Exit code `0` → proceed. Non-zero → `uv` is missing; tell the user to install it (don't auto-run network
installers without a heads-up):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

(`uvx` is an alias for `uv tool run`; if `command -v uv` succeeds, `uvx` works.) Notes: `markitdown` is
v0.1.x and needs **Python ≥3.10** — `uvx` provisions a suitable interpreter itself, so the system Python
version doesn't block it. The **first** `uvx` run pays a one-time cold download of the package + extras (the
`[all]` set is sizeable); subsequent runs hit the `uv` cache and are fast.

## Default output filename

Convert next to the source, reusing the input's stem with a `.md` suffix — `report.pdf` → `report.md` in the
same directory. Pass it explicitly with `-o` so behavior is deterministic:

```bash
uvx --from 'markitdown[all]' markitdown "/path/to/report.pdf" -o "/path/to/report.md"
```

Omitting `-o` writes the conversion to **stdout** (useful for piping or quick inspection, not for saving a
file). Don't clobber an existing `.md` without flagging it. After writing, `Read` the output to confirm it's
non-empty and sane (especially for PDFs, where layout extraction can be lossy).

## High-fidelity / scanned-PDF path (gated, default OFF)

Plain `[all]` uses local text extraction — fine for digital-native PDFs, **but it cannot read scanned/
image-only PDFs** (no embedded text → empty output). For scanned docs, complex tables, or when the user
explicitly wants high-fidelity layout, route the PDF through **Azure Document Intelligence**. Only enable
this when the user asks for it *and* the endpoint env var is set — don't turn it on by default (it's a paid
external service).

Check the endpoint is configured:

```bash
printenv AZURE_DOC_INTEL_ENDPOINT
```

Exit code `0` (and non-empty) → enable it; the extra is `[all,az-doc-intel]` and you pass `-d` plus the
endpoint via `-e`:

```bash
uvx --from 'markitdown[all,az-doc-intel]' markitdown "/path/to/scan.pdf" -d -e "$AZURE_DOC_INTEL_ENDPOINT" -o "/path/to/scan.md"
```

Non-zero/empty `printenv` → the var isn't set; stay on `[all]` and, if the PDF turns out to be scanned
(output comes back empty), tell the user that scanned PDFs need the `az-doc-intel` path with
`AZURE_DOC_INTEL_ENDPOINT` configured.

## Other inputs

- **URL / webpage / YouTube** — pass the URL in place of a file path; `markitdown` fetches HTML (or YouTube
  transcript/metadata) and renders Markdown. Still use `--from 'markitdown[all]'`.
- **Images** — `[all]` extracts EXIF and any embedded text; richer image OCR/captioning can be wired via an
  LLM plugin, but the default local path is metadata + basic text.
- **Audio** — `[all]` includes speech-to-text for transcription to Markdown.
- **ZIP** — recurses into members and concatenates their conversions.

## Etiquette

- Single command per Bash call — never chain conversions with `&&`/`;`. Run one `uvx` invocation per file.
- Always quote both the `--from 'markitdown[all]'` spec and the input/output paths (spaces, brackets).
- Report what was converted, the output path, and any quality caveat (lossy layout, empty scanned-PDF
  output → suggest the az-doc-intel path). Never claim success without reading the result.
