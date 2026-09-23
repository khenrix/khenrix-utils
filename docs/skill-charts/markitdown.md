# markitdown — flow

Prose-only: there is no bundled engine, so every gate here is judgment the skill
documents rather than a script the repo tests. The one rule that matters is never bare
`uvx markitdown` - always the `[all]` extra - and everything else is preflighting `uv`,
requiring consent before audio/video transcription uploads audio to Google, keeping both paid
Azure conversion paths opt-in and endpoint-gated, and
reading the output back before calling it done. Source: `shared/skills/markitdown/SKILL.md`.

```mermaid
flowchart TD
    accTitle: markitdown flow
    accDescr: Every decision here is skill guidance rather than engine code, since markitdown ships no script. Preflight uv, always use the all-extra instead of the bare package, require explicit approval before audio or video transcription uploads audio to Google speech recognition, keep Azure Document Intelligence and Content Understanding opt-in and endpoint-gated, and read the result back before reporting success.

    START([a document, URL, or YouTube<br/>link to read as markdown]) --> G_UV{command -v uv<br/>succeeds?}
    G_UV -- "missing" --> INSTALL_HINT([stop: point to the astral.sh<br/>installer, don't auto-run it])
    G_UV -- "present" --> G_EXTRAS{about to run bare<br/>uvx markitdown, no all-extra?}
    G_EXTRAS -- "yes, catch it" --> CATCH_BARE[base package has no pdf, docx,<br/>xlsx, pptx extras - fails or garbles]
    G_EXTRAS -- "no, reviewed all-extra" --> BUILD_CMD
    CATCH_BARE --> BUILD_CMD[run with all-extra ==0.1.8 -<br/>markitdown INPUT -o OUTPUT.md]
    BUILD_CMD --> LOCAL_DEFAULT[uv 0.12.15 resolves 0.1.8 directly;<br/>pin the reviewed release]
    LOCAL_DEFAULT --> G_EXPLICIT_CU{user explicitly selected<br/>Content Understanding?}
    G_EXPLICIT_CU -- "yes" --> G_CU_READY{Azure upload approved,<br/>endpoint and auth ready?}
    G_CU_READY -- "no" --> STOP_CU([stop: do not upload to Azure])
    G_CU_READY -- "yes" --> RUN_CU
    G_EXPLICIT_CU -- "no" --> G_ARCHIVE{input is a ZIP archive?}
    G_ARCHIVE -- "yes" --> G_ARCHIVE_CONTENTS{local recursive inspection finds<br/>audio/video or an unknown nested archive?}
    G_ARCHIVE_CONTENTS -- "yes" --> G_AUDIO_APPROVAL
    G_ARCHIVE_CONTENTS -- "no" --> G_IMAGE
    G_ARCHIVE -- "no" --> G_AUDIO{input is WAV, MP3,<br/>M4A, or MP4?}
    G_AUDIO -- "yes" --> G_AUDIO_APPROVAL{user explicitly approved upload<br/>to Google speech recognition?}
    G_AUDIO_APPROVAL -- "no" --> STOP_AUDIO([stop: do not run; offer a<br/>user-approved local transcription tool])
    G_AUDIO_APPROVAL -- "yes" --> RUN
    G_AUDIO -- "no" --> G_IMAGE{JPEG / PNG needs OCR or<br/>pixel-content extraction?}
    G_IMAGE -- "yes" --> G_IMAGE_CLOUD{user approved paid Content Understanding,<br/>endpoint and Azure auth available?}
    G_IMAGE_CLOUD -- "no" --> STOP_IMAGE([stop: standard CLI may emit ExifTool metadata;<br/>it does not OCR image pixels])
    G_IMAGE_CLOUD -- "yes" --> RUN_CU
    G_IMAGE -- "no" --> G_HIFI{user explicitly wants<br/>cloud conversion?}
    G_HIFI -- "no" --> RUN[run the conversion]
    G_HIFI -- "yes" --> G_CLOUD{scanned PDF layout or<br/>multimodal / structured fields?}
    G_CLOUD -- "scanned PDF" --> G_DOC_ENDPOINT{Document Intelligence endpoint and Azure auth<br/>ready via key or credential chain?}
    G_CLOUD -- "multimodal / fields" --> G_CU_ENDPOINT{Content Understanding endpoint and Azure auth<br/>ready via key or credential chain?}
    G_DOC_ENDPOINT -- "not ready" --> RUN
    G_DOC_ENDPOINT -- "ready" --> RUN_DOCINTEL[pass -d; endpoint comes from<br/>env or explicit -e]
    G_CU_ENDPOINT -- "not ready" --> RUN
    G_CU_ENDPOINT -- "ready" --> RUN_CU[pass --use-cu; endpoint comes from<br/>env or --cu-endpoint]
    RUN --> G_EMPTY{output empty or garbled,<br/>especially a PDF?}
    RUN_DOCINTEL --> G_EMPTY
    RUN_CU --> G_EMPTY
    G_EMPTY -- "empty / garbled" --> DIAGNOSE[likely scanned or image-only -<br/>point to the gated az-doc-intel path]
    G_EMPTY -- "looks right" --> VERIFY_READ
    DIAGNOSE --> VERIFY_READ[Read the output to confirm<br/>non-empty and sane]
    VERIFY_READ --> DONE([converted file + path +<br/>any quality caveat reported])
```

## Gate evidence

| Gate | Kind | Evidence |
|---|---|---|
| G_UV | agent | `evals/markitdown/evals.json::before attempting the conversion` |
| G_EXTRAS | agent | `evals/markitdown/evals.json::installs the BASE package only (no docx/pdf/office extras) and fails or returns empty/garbled output on real office files` |
| G_AUDIO | agent | `evals/markitdown/evals.json::WAV, MP3, M4A, and MP4 inputs` |
| G_AUDIO_APPROVAL | agent | `evals/markitdown/evals.json::Requires explicit approval before running an audio/video conversion` |
| G_ARCHIVE | agent | `evals/markitdown/evals.json::ZIP conversion recursively dispatches nested members` |
| G_ARCHIVE_CONTENTS | agent | `evals/markitdown/evals.json::requires a local recursive member inspection first` |
| G_EXPLICIT_CU | agent | `evals/markitdown/evals.json::routes supported audio/video to Azure Content Understanding instead` |
| G_CU_READY | agent | `evals/markitdown/evals.json::separate Azure consent, endpoint, and authentication` |
| G_IMAGE | agent | `evals/markitdown/evals.json::can emit ExifTool metadata for JPEG/PNG when ExifTool is available` |
| G_IMAGE_CLOUD | agent | `evals/markitdown/evals.json::explicit Azure Content Understanding` |
| G_HIFI | agent | `evals/markitdown/evals.json::paid external service` |
| G_CLOUD | agent | `evals/markitdown/evals.json::For an explicit Content Understanding run` |
| G_DOC_ENDPOINT | agent | `evals/markitdown/evals.json::AZURE_API_KEY` |
| G_CU_ENDPOINT | agent | `evals/markitdown/evals.json::DefaultAzureCredential` |
| G_EMPTY | agent | `evals/markitdown/evals.json::Explains the default local extraction can't read image-only / scanned PDFs (no embedded text → empty output)` |
