---
name: calculations-and-scripting
description: Run Python calculations and small scripts in a session-isolated workspace, save outputs and export attachments.
requires: [analysis]
plugins: [analysis]
---
# Calculations and scripting

Use `ergo_analysis_run(code=...)` for reproducible calculations, file transformations and plots. It returns a job id; poll `ergo_analysis_status`. Execution is disabled until the host configures an approved isolated controller. Never use host shell as a fallback. No network, credentials, package installation or access to other sessions is available. Installed libraries include Python, pandas, DuckDB, matplotlib, pyarrow and openpyxl.

```python
from decimal import Decimal
from pathlib import Path
principal = Decimal('10000')
rate = Decimal('0.05')
years = 7
balance = principal * (1 + rate) ** years
text = f'Annual compounding, {years} years: {balance:.2f}\n'
print(text)
Path('/workspace/result.txt').write_text(text)
```

Each job is fresh; variables disappear between jobs. Files under `/workspace` persist for this session and expire after configurable idle retention (default 30 days). Use flat safe filenames. Python may modify its own workspace, so preserve input copies and reopen changed datasets to obtain a new version. Root is read-only and `/tmp` is bounded temporary memory. Jobs have 60-second runtime, 512 MiB memory, one CPU, bounded workspace storage and 48 KiB captured output. Prefer small printed summaries and saved files over dumping arrays. On timeout/output-limit inspect the status; do not claim completion from partial output.

Use `ergo_analysis_files` to inspect filenames, `ergo_analysis_import` for permitted attachments, and `ergo_analysis_export(file='result.txt')` to make a durable chat attachment. Exports survive workspace expiry. For data work load `data-analysis` and push projection, filtering and aggregates into structured selects before expanding model context. Avoid embedding untrusted data in code strings; import it as a file and read with the appropriate library.

Protected private-export copies cannot be imported into reusable workspaces; their no-forward restriction must survive transformations. Ask for a directly uploaded authorized source file instead.
