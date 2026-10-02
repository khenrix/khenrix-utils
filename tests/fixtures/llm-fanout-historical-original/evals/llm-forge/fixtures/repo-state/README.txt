A CAPTURE OF ONE SMALL REPOSITORY, not a live git directory. Each file is the recorded
output of the command its name states, taken at the same moment, so the four views are
consistent with each other:

  Makefile         the repository's own gate — `make verify` is the confirmed verify command
  gitignore.txt    the contents of .gitignore
  git-ls-files.txt `git ls-files` — what is TRACKED
  git-status.txt   `git status --porcelain` — what is dirty or untracked (ignored files do
                   not appear in this form at all)
  tree.txt         the working tree on disk, including the ignored files

Read them together. `src/app.py` is tracked AND modified; `scratch/notes.md` is untracked;
`dist/bundle.js` exists on disk, is ignored, and is therefore in tree.txt and in neither
git-ls-files.txt nor git-status.txt.
