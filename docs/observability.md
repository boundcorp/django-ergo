# Observability: read-only Grafana and Prometheus

The `observability` plugin lets a bot find Grafana dashboards, run bounded PromQL,
check Prometheus target health, build deep links and draw a chart that arrives in
the chat as a real attachment. Everything is a GET. There is no Grafana write path:
no dashboard save, no service accounts, no alert changes, no admin Prometheus API.

The library is `django_ergo.observability` (`http`, `timeutil`, `prometheus`,
`grafana`, `charts`, `text`); the plugin in `django_ergo.plugins.observability` only
binds it to a bot, a token and the chat.

## Configuration

Profiles belong to the host, in `bot.yaml`. The model never supplies a URL, a header
or a credential name; a selected datasource uid must be allowed by the profile.

```yaml
plugins:
  - name: observability
    root_only: false # optional (default false)
    profiles:
      prod:
        grafana_url: http://grafana.monitoring.svc # fixed origin the server talks to
        public_url: https://grafana.example.com # origin used in links (default: grafana_url)
        token_env: GRAFANA_VIEWER_TOKEN # read at call time with ctx.secret()
        token_file: /run/secrets/grafana-token # optional, read at call time
        datasources: [prometheus] # datasource uids the model may query
        mode: proxy # proxy (default) or direct
        prometheus_url: http://prometheus.monitoring.svc:9090 # direct mode only
        renderer: false # true only if Grafana has a working image renderer
        ownership: # optional; unknown ownership is reported as unknown
          - match_uid_prefix: "team-"
            repo: example-org/infra
            path_glob: "dashboards/*.yaml"
```

- `grafana_url`, `public_url`, `prometheus_url` are `http(s)` URLs without credentials,
  query or fragment. Unknown keys are rejected at load.
- **proxy** mode (default) queries through Grafana's datasource proxy
  (`/api/datasources/proxy/uid/<uid>/api/v1/...`) with the token as a Bearer header.
  **direct** mode queries `prometheus_url` with no credential. Configure at most
  one datasource uid in direct mode: it identifies that fixed origin, and a supplied
  uid must match it. With none configured, do not supply a datasource uid. There is
  never a fallback from one mode to the other, in particular not after a 401 or 403.
- Use a Grafana **Viewer** service-account token. Viewer can query more datasources
  than Prometheus; `datasources` is the plugin-level allowlist, not credential-level
  isolation. Enable server-side rendering only when Grafana's own token permissions
  are also appropriate for every datasource the token can reach.
  Dashboard inspection and Grafana rendering are separate requests, so the plugin
  cannot prevent a dashboard or datasource from changing between them. Use
  server-side datasource permissions or a dedicated Grafana organization/proxy
  when credential-level isolation is required.
  The renderer rejects library panels, unresolved or mixed datasource references,
  and dashboards with variables or annotations; provide a dashboard link instead.
- The token comes from `token_env` (per-user `NAME__USER` first, see `ToolContext.secret`),
  else `token_file`, on every call, so rotation needs no restart. It is sent only to
  the configured origin; redirects are never followed.
- `ownership` rules map a dashboard uid (`match_uid` or `match_uid_prefix`) to
  `repo`/`path_glob`/`source` hints. They are config, never guessed.

## Tools

| Tool | Does |
| --- | --- |
| `ergo_grafana_search` | `kind=dashboards`: search with uid, title, tags, folder, link. `kind=datasources`: name, uid, type, default of the allowed datasources only. |
| `ergo_grafana_dashboard` | Panels, queries, datasource uids, variables, `provisioned`, owner (from `ownership`, else `unknown`) and how to change it (PR to the owning file). |
| `ergo_prom_query` | Instant or range PromQL with explicit `tz`; prints UTC and local endpoints, step and source. |
| `ergo_prom_targets` | `/api/v1/targets` scrape health (job, instance, health, last scrape, last error), separate from optional `up` values. |
| `ergo_grafana_link` | Dashboard or panel link with bounded, encoded `from`/`to`/`timezone`/`var-*` parameters; oversized links are rejected. |
| `ergo_prom_chart` | PNG line chart of a range query, saved as a chat attachment (`source="bot"`, metadata: query, range, tz, step, profile, datasource, link). |
| `ergo_grafana_panel_render` | Only when a profile sets `renderer: true`: renders an authorized panel, rejects unresolved datasource references, and attaches a validated, pixel-only PNG of the requested size. |

Errors come back as `ERROR [code] message` with `code` one of `not_configured`,
`unauthorized` (401), `forbidden` (403), `not_found`, `invalid_query` (400/422 and bad
arguments), `rate_limited`, `upstream_unavailable`, `timeout`, `bounds_exceeded`.
Messages never contain upstream HTML, headers or tokens.

## Bounds

| Resource | Limit |
| --- | --- |
| Window | 7 days; end not before start, not in the future |
| Time zone | explicit IANA `tz`; naive local times in a DST gap or fold are rejected (add an offset) |
| Step | minimum 15s; default allows at most 1000 returned points/series; an explicit step allows at most 2000 points and is rejected if the requested window exceeds that limit, never coarsened |
| Result | 100 series, 100,000 float and histogram samples combined; overflow fails, nothing is truncated |
| Expression | 8 KiB |
| HTTP | 3s connect, 15s read, 20s deadline during network reads (see below), Prometheus `timeout=8s`, 4 MiB after decompression, no retries; JSON nesting at most 100 and valid Unicode scalar text required |
| Tool text | 32 KiB |
| Chart | up to 1600x900, PNG at most 4 MiB, one chart per call |
| Inventory | 100 per dashboard page, 1000 per search or datasource response; oversized datasource inventories fail, and dashboard summaries mark omitted panels and targets |

The HTTP deadline is enforced during header and body reads. System DNS resolution
and multi-address connection/TLS setup can exceed it; it is not a hard process-wide
deadline.

## Semantics worth knowing

- An empty result is **absent**, not zero. A zero sample is zero. Gaps stay gaps.
  NaN/Inf are annotated. Native histograms are reported as unsupported for plotting.
- Target health is scrape health. `up=1` does not prove the application works, and a
  missing target is a monitoring gap.
- The chart is a data chart, labeled "Data chart, not a Grafana panel screenshot", with
  query, range, timezone, step and source in the image. Panels that cannot be
  reproduced (other datasources, transformations, variables) get a link, not an imitation.
- Grafana without a renderer can answer 200 with a small "No image renderer available"
  PNG. The render tool validates the complete raster and requested dimensions, then
  reencodes the pixels without ancillary metadata. Sensitive information drawn into
  image pixels is not removed.

## Install

Charts need matplotlib (Agg, imported lazily when a chart is drawn):

```bash
pip install 'django-ergo[observability]'
```

Everything else works without it; a chart call then returns `not_configured`.
Ergonaut's image installs the extra (`ergonaut/requirements.freeze.txt` pins matplotlib
and its dependencies; regenerate with `make freeze` in `ergonaut/`).
