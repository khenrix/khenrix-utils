---
name: markitdown
description: >-
  Convert documents to Markdown — PDF, DOCX, XLSX, PPTX, HTML, CSV/JSON/XML, images (metadata;
  explicit cloud or Python API extraction),
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
(EXIF metadata; explicit cloud or Python API image extraction), audio (speech→text with consent), ZIP
(recurses members), and YouTube URLs.

## The one rule that matters

**Always invoke with `--from 'markitdown[all]==0.1.8'`. Never bare `uvx markitdown`.**

```bash
uvx --from 'markitdown[all]==0.1.8' markitdown "<INPUT>" -o "<OUTPUT.md>"
```

Bare `uvx markitdown` resolves the **base** package only — it has no PDF/DOCX/XLSX/PPTX extras and **fails on
real office files** (or silently emits empty/garbled output). The `[all]` extra is what pulls in `pdfminer`,
`mammoth` (DOCX), `openpyxl`, `python-pptx`, etc. This is the single most common mistake; get it right every
time. Quote `'markitdown[all]==0.1.8'` so the shell doesn't glob the brackets.

### Current resolver behavior

With the repository-pinned `uv 0.12.15`, the unpinned `[all]` command resolves directly to
MarkItDown 0.1.8 and `azure-ai-contentunderstanding 1.2.0b3`. No prerelease flag or fallback
workaround is needed. Copy-paste commands still pin `==0.1.8` so a future unreviewed release
cannot enter a conversion silently. `[all]` installs every format extra plus the Azure client libraries,
but it does not select an Azure converter: documents and images use their local converters unless `-d`
or `--use-cu` is passed with its endpoint. Audio and video are different: the standard transcriber calls
Google's speech-recognition service. ZIP archives recursively dispatch members, so an audio/video member
can make an otherwise local archive conversion call Google too. Apply the approval and archive gates
below. URL and YouTube inputs still fetch the named remote resource.

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

(`uvx` is an alias for `uv tool run`; if `command -v uv` succeeds, `uvx` works.) MarkItDown 0.1.8 supports
**Python 3.10 through 3.14**. `uvx` provisions a suitable interpreter itself, so the system Python version
doesn't block it. The **first** `uvx` run pays a one-time cold download of the package + extras (the `[all]`
set is sizeable); subsequent runs hit the `uv` cache and are fast.

## Default output filename

Convert next to the source, reusing the input's stem with a `.md` suffix — `report.pdf` → `report.md` in the
same directory. Pass it explicitly with `-o` so behavior is deterministic:

```bash
uvx --from 'markitdown[all]==0.1.8' markitdown "/path/to/report.pdf" -o "/path/to/report.md"
```

Omitting `-o` writes the conversion to **stdout** (useful for piping or quick inspection, not for saving a
file). Don't clobber an existing `.md` without flagging it. After writing, `Read` the output to confirm it's
non-empty and sane (especially for PDFs, where layout extraction can be lossy).

The Python API returns Markdown on `result.markdown`:

```python
from markitdown import MarkItDown

result = MarkItDown().convert("/path/to/report.pdf")
print(result.markdown)
```

## High-fidelity / scanned-PDF path (gated, default OFF)

Plain `[all]` uses local text extraction — fine for digital-native PDFs, **but it cannot read scanned/
image-only PDFs** (no embedded text → empty output). For scanned docs, complex tables, or when the user
explicitly wants high-fidelity layout, route the PDF through **Azure Document Intelligence**. Only enable
this when the user asks and an endpoint plus Azure auth are available — don't turn it on by default (it's
a paid external service).

MarkItDown reads the Document Intelligence endpoint from
`MARKITDOWN_DOCINTEL_ENDPOINT`. Check it before opting in:

```bash
printenv MARKITDOWN_DOCINTEL_ENDPOINT
```

Exit code `0` (and non-empty) → enable it with `-d`; `[all]` already includes the
`az-doc-intel` extra:

```bash
uvx --from 'markitdown[all]==0.1.8' markitdown "/path/to/scan.pdf" -d -o "/path/to/scan.md"
```

Non-zero/empty `printenv` → the var isn't set; stay on `[all]` and, if the PDF turns out to be scanned
(output comes back empty), tell the user that scanned PDFs need the `az-doc-intel` path with
`MARKITDOWN_DOCINTEL_ENDPOINT` configured. An explicit endpoint can instead be passed with
`-e "<document-intelligence-endpoint>"`.

Two more OCR options are available. **Azure Content Understanding** is a separate paid,
gated service for multimodal conversion and structured fields. Set
`MARKITDOWN_CU_ENDPOINT`, then pass `--use-cu`; or pass the endpoint explicitly with
`--use-cu --cu-endpoint "<content-understanding-endpoint>"`. It is mutually exclusive
with `-d` and remains off unless selected. The narrower install extra is
`az-content-understanding`, while `[all]` already contains it. The **`markitdown-ocr`
plugin** uses LLM vision, but the CLI exposes no `--llm-client` or `--llm-model` options;
use its Python API when that path is required. Azure Document Intelligence remains the
primary scanned-PDF route here. Both Azure paths also require authentication:
`AZURE_API_KEY` when configured, otherwise Azure's `DefaultAzureCredential` chain.

## Other inputs

- **URL / webpage / YouTube** — pass the URL in place of a file path; `markitdown` fetches HTML (or YouTube
  transcript/metadata) and renders Markdown. Still use `--from 'markitdown[all]==0.1.8'`.
- **Images** — the standard CLI path can emit ExifTool metadata for JPEG and PNG when ExifTool is
  available; without it, the conversion may emit no useful content. It does not OCR image pixels.
  MarkItDown's Python `ImageConverter` can describe pixels only when both `llm_client` and
  `llm_model` are supplied, and the CLI has no flags for those Python API arguments. For OCR or image
  content extraction, use the explicit, paid Content Understanding path only after the user approves it
  and its endpoint and Azure authentication are available.
- **Audio / video** — for the standard command without `--use-cu`, tell the user that converting a
  **WAV, MP3, M4A, or MP4** calls SpeechRecognition's `recognize_google`, which uploads the audio to
  Google's speech-recognition service. Obtain **explicit approval** for that Google upload before running
  it. The standard CLI has no local-only transcription switch. Explicit `--use-cu` routes supported
  audio/video to Azure Content Understanding instead; get separate approval for Azure and require its
  endpoint and authentication, but do not request Google approval for that Azure path. Without approval
  for the selected service, do not run MarkItDown; offer a user-approved local transcription tool.
- **ZIP** — recursively dispatches members to their normal converters. Before converting, inspect archive
  members locally and recursively. If a member is WAV, MP3, M4A, or MP4, apply the selected Google or
  Azure consent gate above. Treat a nested archive you cannot fully inspect the same way: stop and ask for
  approval before conversion rather than risk a hidden recording being uploaded.

## Etiquette

- Single command per Bash call — never chain conversions with `&&`/`;`. Run one `uvx` invocation per file.
- Always quote both the `--from 'markitdown[all]==0.1.8'` spec and the input/output paths (spaces, brackets).
- Report what was converted, the output path, and any quality caveat (lossy layout, empty scanned-PDF
  output → suggest the az-doc-intel path). Never claim success without reading the result.
- markitdown reads/writes with your process privileges and will fetch URLs and recurse into ZIP members —
  don't convert untrusted inputs from inside sensitive directories.
