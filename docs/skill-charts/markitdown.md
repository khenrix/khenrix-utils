# markitdown — flow

Prose-only: there is no bundled engine, so every gate here is judgment the skill
documents rather than a script the repo tests. The one rule that matters is never bare
`uvx markitdown` - always the `[all]` extra - and everything else is preflighting `uv`,
picking the right pin when 0.1.6 is needed, keeping the paid Azure OCR path opt-in, and
reading the output back before calling it done. Source: `shared/skills/markitdown/SKILL.md`.

```mermaid
flowchart TD
    accTitle: markitdown flow
    accDescr: Every decision here is skill guidance rather than engine code, since markitdown ships no script. Preflight uv, always the all-extra never the bare package, pin 0.1.6 with the non-prerelease extras when its PDF fixes are specifically needed, keep the paid Azure Document Intelligence OCR path opt-in and endpoint-gated, and read the result back before reporting success.

    START([a document, URL, or YouTube<br/>link to read as markdown]) --> G_UV{command -v uv<br/>succeeds?}
    G_UV -- "missing" --> INSTALL_HINT([stop: point to the astral.sh<br/>installer, don't auto-run it])
    G_UV -- "present" --> G_EXTRAS{about to run bare<br/>uvx markitdown, no all-extra?}
    G_EXTRAS -- "yes, catch it" --> CATCH_BARE[base package has no pdf, docx,<br/>xlsx, pptx extras - fails or garbles]
    G_EXTRAS -- "no, already all-extra" --> BUILD_CMD
    CATCH_BARE --> BUILD_CMD[run with the all-extra -<br/>markitdown INPUT -o OUTPUT.md]
    BUILD_CMD --> G_NEED016{task specifically needs 0.1.6 -<br/>PDF memory / RecursionError fixes?}
    G_NEED016 -- "no" --> NOTE_BACKTRACK[plain all-extra silently backtracks<br/>to 0.1.5 - fine for everyday use]
    G_NEED016 -- "yes" --> PIN_016[pin the standard extras + 0.1.6 -<br/>no prerelease override needed]
    NOTE_BACKTRACK --> G_HIFI{user explicitly wants<br/>scanned / high-fidelity handling?}
    PIN_016 --> G_HIFI
    G_HIFI -- "no" --> RUN[run the conversion]
    G_HIFI -- "yes" --> G_ENDPOINT{AZURE_DOC_INTEL_ENDPOINT<br/>set?}
    G_ENDPOINT -- "unset" --> RUN
    G_ENDPOINT -- "set" --> RUN_DOCINTEL[add the az-doc-intel extra,<br/>pass -d and -e the endpoint]
    RUN --> G_EMPTY{output empty or garbled,<br/>especially a PDF?}
    RUN_DOCINTEL --> G_EMPTY
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
| G_NEED016 | agent | `evals/markitdown/evals.json::so the resolver quietly falls back to 0.1.5` |
| G_HIFI | agent | `evals/markitdown/evals.json::gated / opt-in (requires the endpoint env var set; a paid external service), not the default` |
| G_ENDPOINT | agent | `evals/markitdown/evals.json::Recommends the Azure Document Intelligence path` |
| G_EMPTY | agent | `evals/markitdown/evals.json::Explains the default local extraction can't read image-only / scanned PDFs (no embedded text → empty output)` |
