# Extracting this into its own GitHub repository

This directory (`ev-plug-charging/`) is a complete, self-contained HACS
repository, built as a subdirectory of the Home Assistant config repo it was
ported from. To lift it into its own repository:

```bash
# From the ev-plug-charging/ directory:
git init
git add -A
git commit -m "Initial commit: EV Plug Charging, ported from packages/ev_charging.yaml"
git branch -M main
git remote add origin git@github.com:<you>/ha-ev-plug-charging.git
git push -u origin main
```

That's it — every file needed (manifest, hacs.json, CI workflows, tests,
docs) is already in place and does not reference anything outside this
directory.

## After pushing

- Add the new repository to HACS as a custom repository (category:
  Integration) until/unless it's accepted into the default HACS store.
- The `.github/workflows/validate.yml` workflow (hassfest + the HACS
  validation action) and `test.yml` (ruff + pytest) will run on push and on
  a weekly schedule — check they're green on the new repo; branch
  protection/required-status-checks can be configured from there.
- Update `manifest.json`'s `documentation`/`issue_tracker` URLs and this
  file's placeholder remote if the final repository name differs from
  `ha-ev-plug-charging`.
- `AGENTS.md`, `.agents/skills/`, `CLAUDE.md` and `.claude/skills/` travel
  with the repository unchanged — they cite the upstream design record
  (`docs/ev-charging-requirements.md`) and the YAML package by name only,
  never as a path, so nothing breaks when this directory becomes its own
  repository. If the design record is ever published alongside this code,
  that is the one place worth turning those names into links.
