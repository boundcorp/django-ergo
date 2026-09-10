# Preserved Ergo source evidence

This is a byte-identical, allowlisted preservation set created before any possible
worktree cleanup. `manifest.json` maps original paths/revisions to SHA-256-named
UTF-8 `.txt` objects. The suffix deliberately prevents old tests, migration modules
and workflow configurations from executing or becoming part of the package.
These are historical evidence, not current architecture or accepted future work.
References in original documents are preserved verbatim and may be relative to
their original locations.

The 159 source-state records deduplicate to 63 objects:

- All 39 previously inventoried fs-vector files, checked against the September 9
  reconciliation hashes, including its three review reports and workflow document.
- All six held Cabal planning files, checked against the prior preservation review.
  This supplies verified Git backups for the June helper research and May 26 gstack
  reevaluation, which previously had no verified backups. Attachment metadata alone
  was not treated as backup evidence for any of the six.
- Changed file states relative to committed base `5f3e5de` from temporary synthesis
  commits `bbd3eef`, `0fd6cf5` and `e0ff440`, including seven unique generated wiki
  pages. Keeping these snapshots does not approve that old wiki as current guidance.
- Main's original untracked `docs/plan-fs.md`.

## Held planning documents

| Original document | Verified backup |
| --- | --- |
| 2026-06-13-structured-call-chatbot-helper-research.md | [exact copy](objects/cb3a0d66a817ac3631b41c9237979113c2670f91627d96eeb96654cda3cd77be.txt) |
| adoption-plan-gaps-checklist.md | [exact copy](objects/b892d65203da4257e1116c40169ffd2a39cb982f3f98712b52d585bbad7038c7.txt) |
| gstack-new-features-reevaluation-2026-05-26.md | [exact copy](objects/af502c183dd6ed42414d9cb3be21d8ea8628d20a15a2244ebb7e24fb761066fb.txt) |
| investigation-findings-adoption-gaps.md | [exact copy](objects/4e4a7794ab26504b39ac6e6e1c5f25663c96368ad2ace420fe57c58ca2aa4bcf.txt) |
| prioritized-adopt-learn-avoid-roadmap.md | [exact copy](objects/06a7ae7cecdecf1751e69f6badccad14f8704dbb63a1d106fe363dc8a71d2d93.txt) |
| remaining-gaps-next-experiments.md | [exact copy](objects/693dd90e648f9beb8f5b8fa9bfeb0f76c4a8a7888dbb4f450145d093b4214692.txt) |

## Verification and retention

No original file was moved, edited or removed to create this set. No `.cabal`, auth,
raw session/transcript, database, environment or provider-key material is included.
The allowlist excludes ignored virtual environments, caches and generated metadata.
Token/private-key/credential-URL pattern checks found no candidate secrets in the
selected source contents; this does not imply a general secret audit of worktrees.

Verify every object from the repository root:

```sh
python - <<'PY'
import hashlib
import json
from pathlib import Path

manifest = json.loads(Path('docs/preservation/2026-09-10/manifest.json').read_text())
for record in manifest['records']:
    content = Path(record['backup']).read_bytes()
    assert len(content) == record['bytes'], record['source']
    assert hashlib.sha256(content).hexdigest() == record['sha256'], record['source']
print('Verified', len(manifest['records']), 'source-state records')
PY
```

An absent file in the manifest is not proof it is disposable. Before removing any
checkout, recheck its tracked/untracked/ignored state and all original hashes,
confirm no live Orca terminal/task owns it, and use the installed version's public
Orca worktree-removal command. Preserve uncertain state instead of forcing removal.
In particular the two held Cabal checkouts still contain excluded operational/auth
state: this planning backup does **not** authorize deleting that state.
