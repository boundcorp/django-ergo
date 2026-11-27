---
name: data-analysis
description: Analyze CSV, Parquet and Excel with full-data filters, grouped aggregates, bounded results and isolated pandas.
requires: [analysis, attachments]
plugins: [analysis, attachments]
---
# Data analysis

The analysis plugin must be explicitly enabled by the host with an approved isolated controller. If unavailable, explain that configuration is needed. Never substitute host bash for isolation. Workspaces are lazy per session, have no network or credentials, and expire after about 30 idle days. Export results before relying on them long term.

1. Find an attachment with `ergo_attachments_list`. Import its id using `ergo_analysis_import`; this streams bytes without putting the data in context. Keep its returned file handle.
2. Call `ergo_analysis_dataset(file=handle)` and poll `ergo_analysis_status(job=...)`. The result gives a dataset id, schema, version and exact source row count. CSV, Parquet and XLSX are supported; Excel currently uses the first worksheet. Parsing runs only in the isolated job. Schema failures or resource failures are errors, never an implicit sample.
3. Inspect a bounded sample with `ergo_analysis_select(dataset=id, query={"columns":["date","region","revenue"],"limit":10})`; poll the job. Do not retrieve the entire table to discover its shape.
4. Filter and aggregate before returning rows. For example:

```json
{"columns":["region","revenue"],"where":{"op":"and","args":[{"op":"ge","column":"revenue","value":100},{"op":"in","column":"region","values":["west","east"]},{"op":"not_null","column":"revenue"}]},"group_by":["region"],"aggregates":[{"op":"sum","column":"revenue","as":"total"},{"op":"count","column":"*","as":"records"}],"sort":[{"column":"total","direction":"desc"}],"limit":20}
```

Comparisons: `eq`, `ne`, `lt`, `le`, `gt`, `ge` use `value`; `in` uses `values` (at most 100); `is_null` and `not_null` have no value. Combine with `and`/`or` `args` or `not` `arg`. SQL NULL semantics apply: `ne` does not include nulls, so use `or` with `is_null` if needed. Aggregates: count, count_distinct, sum, avg, min, max. Select at most 128 columns, 32 grouping/aggregate/sort entries and 1000 rows. Raw SQL and joins are not exposed; use isolated pandas for joins or derived fields.

The result has `total_rows` (exact count of filtered output rows/groups), `source_rows`, `count_kind`, `truncated`, provenance and `cursor`. Aggregates use the full filtered dataset. Row and byte caps both apply; a single oversized row errors. Numeric decimal/float/date values may be serialized as strings. To continue, pass the same dataset and exact query with the returned cursor. Cursors expire after a day and bind workspace, dataset version and query. Changed source/snapshot bytes invalidate the query instead of mixing pages. Sorting includes all projected columns for deterministic pagination; identical projected rows are indistinguishable.

For joins, cleaning, advanced statistics or plotting, submit Python:

```python
from pathlib import Path
import pandas as pd
import matplotlib.pyplot as plt
root = Path('/workspace')
a = pd.read_csv(root / 'import-RETURNED.csv')
b = pd.read_excel(root / 'import-OTHER.xlsx', sheet_name='Sheet1')
joined = a.merge(b, on='customer_id', how='left', validate='many_to_one')
summary = joined.loc[joined['revenue'].ge(100)].groupby('region', dropna=False)['revenue'].agg(['size','sum','mean'])
print({'source_rows': len(a), 'joined_rows': len(joined), 'groups': len(summary)})
print(summary.head(20).to_string())
summary.to_csv(root / 'summary.csv')
summary['sum'].plot.bar()
plt.tight_layout()
plt.savefig(root / 'revenue.png')
```

Poll and inspect the exit reason. Export `summary.csv` and `revenue.png` with `ergo_analysis_export`; report attachment ids. Never claim calculations succeeded when the job failed, timed out or truncated its output. State assumptions about sheet selection, missing values, joins, currency and dates; label samples explicitly and retain the original row counts.

Protected private-export copies cannot be imported into reusable workspaces; their no-forward restriction must survive transformations. Ask for a directly uploaded authorized source file instead.
