# One Pace — Plex custom metadata provider

This directory builds a JSON catalog from the [`One Pace/`](../One%20Pace/) NFO files and ships it behind a [Plex-compatible metadata provider](https://developer.plex.tv/pms/index.html#section/API-Info/Metadata-Providers) on **Cloudflare Workers**. That lets Plex pull show/season/episode metadata over HTTPS (including **`POST` matching**), which static hosting alone cannot do.

Upstream / fork: [snoopyh42/one-pace-for-plex](https://github.com/snoopyh42/one-pace-for-plex).

## Why not GitHub-only?

- **GitHub Actions** is ideal for building `catalog.json` on every push (see [`.github/workflows/build-catalog.yml`](../.github/workflows/build-catalog.yml)).
- **GitHub Pages** only serves static files; Plex’s provider contract requires a **`POST /library/metadata/matches`** endpoint, so a small edge runtime (**Worker** or **Pages Function**) is still required somewhere.
- **Cloudflare** (Workers + KV + your domain) is a minimal, inexpensive fit.

---

## First-time setup: Cloudflare Worker + KV

You are **not** creating an empty Worker in the dashboard first. This repo **is** the Worker: [`cloudflare/src/index.ts`](cloudflare/src/index.ts). You deploy it with **Wrangler** (CLI) from your machine or via **GitHub Actions**. The dashboard is where you grab **Account ID**, create **API tokens** and **KV namespaces**, and optionally attach a **custom domain**.

### 1. Cloudflare Account ID

1. Log in to the [Cloudflare dashboard](https://dash.cloudflare.com).
2. On the **Workers & Pages** overview (or the home dashboard), copy **Account ID** from the right-hand sidebar (or from **Manage account → Workers** — it is a 32-character hex string).
3. You will paste this into GitHub as `CLOUDFLARE_ACCOUNT_ID` (below).

### 2. Cloudflare API token (for Wrangler + GitHub Actions)

1. Open **[My Profile → API Tokens](https://dash.cloudflare.com/profile/api-tokens)** (or **Manage account → API Tokens**).
2. **Create Token** → use the template **“Edit Cloudflare Workers”** (includes Workers Scripts + Workers KV), **or** create a **Custom token** with at least:
   - **Account** → **Workers Scripts** → **Edit**
   - **Account** → **Workers KV Storage** → **Edit**
3. Under **Account Resources**, include **your** account.
4. Create the token and copy it **once** (it is not shown again). This becomes `CLOUDFLARE_API_TOKEN` in GitHub.

For **local** `wrangler deploy`, either export the token or run `npx wrangler login` (OAuth) instead of a token; CI always uses a token via secrets.

### 3. Workers KV namespace

1. In the dashboard: **Workers & Pages** → **KV** (or **Storage & databases** → **KV**, depending on UI).
2. **Create a namespace**. Choose a name (e.g. `onepace-catalog`).
3. After creation, open the namespace and copy its **Namespace ID** (UUID). Put that UUID in [`cloudflare/wrangler.toml`](cloudflare/wrangler.toml) under `[[kv_namespaces]]` → `id` (this fork uses **`ccd2310d9e404fef8a008ddfd63788f6`** — change it if you created your own).

The Worker reads the catalog from KV using the binding name **`CATALOG`** (defined in `wrangler.toml`). The **key** inside that namespace must be **`catalog`** (the JSON blob).

### 4. Deploy the Worker (first time)

**Option A — GitHub Actions (recommended after secrets are set)**  
Push to **`main`**. The workflow builds `catalog.json`, runs `wrangler kv key put catalog …`, then `wrangler deploy`. See [GitHub Actions secrets](#github-actions-secrets-continuous-deploy).

**Option B — Your laptop**

```bash
# From repo root: build catalog
cd plex-provider
uv sync --group dev
uv run python build_catalog.py --root ..

# Upload catalog to KV (use YOUR namespace ID if different)
cd cloudflare
npm ci
export CLOUDFLARE_API_TOKEN="your-token"   # or: npx wrangler login
npx wrangler kv key put catalog --namespace-id="ccd2310d9e404fef8a008ddfd63788f6" --path=../catalog.json

# Deploy Worker (uses wrangler.toml name + KV binding)
npx wrangler deploy
```

After deploy, the CLI prints a **`*.workers.dev`** URL. Test:

```bash
curl -sS "https://<your-worker>.<your-subdomain>.workers.dev/" | head
```

You should see JSON with a top-level **`"MediaProvider"`** key. Until the **`catalog`** key exists in KV, the Worker returns **503**.

### 5. Custom domain (CI: secret or variable; local: `wrangler.toml`)

The public hostname does **not** need to live in git. For **GitHub Actions**, set either:

- **Repository secret** **`PLEX_PROVIDER_HOSTNAME`** — value is your FQDN only, e.g. `onepaceplex.clevername.top` (good if you want it out of **Settings → Variables** and masked in logs where possible), or  
- **Repository variable** **`PLEX_PROVIDER_HOSTNAME`** — same value if you treat the hostname as non-sensitive config (it becomes public once DNS exists anyway).

The workflow injects a **[Workers Custom Domain](https://developers.cloudflare.com/workers/configuration/routing/custom-domains/)** block into `wrangler.toml` before **`wrangler deploy`**. Secret wins if both are set. If neither is set, deploy still runs but only on **`*.workers.dev`**.

**Local `wrangler deploy`:** add the block yourself in [`cloudflare/wrangler.toml`](cloudflare/wrangler.toml) (see comments in that file), or run the same inject snippet once—CI-managed block is stripped and rewritten each run by a marker, so do not rely on hand-editing the checked-in file for the same hostname as CI.

**Requirements**

- The DNS zone (e.g. **`clevername.top`**) must be on the **same** Cloudflare account as the Worker.
- Your API token must be allowed to manage Workers **and** the zone’s custom hostnames. If deploy errors on the custom domain, extend the token with **Zone** → **Workers Routes** → **Edit** and **Zone** → **DNS** → **Edit** for that zone when needed.

Verify after deploy (use your real host):

```bash
curl -sS "https://YOUR_FQDN/" | head
```

You still get **`*.workers.dev`** (e.g. [`onepace-plex-provider.snoopyh42.workers.dev`](https://onepace-plex-provider.snoopyh42.workers.dev/)) unless you set `workers_dev = false`.

---

## GitHub Actions secrets (continuous deploy)

Add these under **GitHub repo → Settings → Secrets and variables → Actions → New repository secret**. Names must match **exactly** (case-sensitive).

| Secret | Where to get it |
|--------|------------------|
| **`CLOUDFLARE_ACCOUNT_ID`** | Cloudflare dashboard → **Account ID** (32-character hex). See **§1** above. |
| **`CLOUDFLARE_API_TOKEN`** | [API Tokens](https://dash.cloudflare.com/profile/api-tokens) → **Edit Cloudflare Workers** template (or custom token with Workers Scripts + KV Storage **Edit**). See **§2** above. |
| **`CLOUDFLARE_KV_NAMESPACE_ID`** | KV namespace UUID — must match `id` under `[[kv_namespaces]]` in `wrangler.toml`. See **§3** above. |

Optional (custom hostname, not in git):

| Secret *or* variable | Purpose |
|----------------------|---------|
| **`PLEX_PROVIDER_HOSTNAME`** | FQDN for the metadata provider (e.g. `onepaceplex.clevername.top`). Set as **secret** or **Actions variable**; workflow injects `[[routes]]` + `custom_domain = true` before deploy. |

The workflow [`.github/workflows/build-catalog.yml`](../.github/workflows/build-catalog.yml) runs **build** on every push/PR. The **deploy** job also starts on pushes to **`main`**; it checks that all three secrets are non-empty (GitHub does not allow `secrets` in **job-level** `if`, so the workflow uses a small gate step instead). When ready, it will:

1. Rebuild and validate `catalog.json`.
2. Optionally inject a Custom Domain route from **`PLEX_PROVIDER_HOSTNAME`** (secret or variable).
3. `wrangler kv key put catalog --namespace-id="$CLOUDFLARE_KV_NAMESPACE_ID"` → refreshes KV.
4. `npx wrangler deploy` → updates the Worker, KV binding, and routes (injected + any you keep in `wrangler.toml` for local use).

If any secret is missing, the deploy job logs a **notice** and skips KV upload and Worker deploy (job still succeeds). PRs from forks never receive secrets, so deploy steps are skipped there too.

## Build catalog locally (using uv — no packages on system Python)

From the **repository root**:

```bash
cd plex-provider
uv sync --group dev
uv run python build_catalog.py --root ..
uv run python -m check_jsonschema --schemafile catalog.schema.json catalog.json
```

`build_catalog.py` uses only the Python standard library. Validation uses a **project venv** managed by `uv`, not `pip install` into the system interpreter.

## Register the provider on Plex PMS

Requires a Plex Media Server version that supports **metadata agent providers** (see [Plex PMS API](https://developer.plex.tv/pms/index.html)).

1. Register the provider base URL (must return `MediaProvider` JSON on **`GET /`**):

   ```http
   POST /media/providers/metadata?uri=https://onepaceplex.clevername.top/
   ```

   Use your real hostname if different. The URI should match the HTTPS origin where the Worker serves the root `MediaProvider` document.

2. In Plex, configure a **metadata agent provider group** so this provider can coexist with TMDB/TVDB where needed (combined groups must expose compatible types; this provider declares **show / season / episode** only).

Exact UI steps depend on your PMS version; refer to current Plex docs for “metadata providers” / “agent groups”.

## API surface (implemented in the Worker)

Aligned with Plex’s [Metadata Providers](https://developer.plex.tv/pms/index.html#section/API-Info/Metadata-Providers) doc:

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | `MediaProvider` discovery document |
| `GET` | `/library/metadata/{ratingKey}` | Single item; `includeChildren=1` for nested children |
| `GET` | `/library/metadata/{ratingKey}/children` | Paged children (`X-Plex-Container-Start` / `X-Plex-Container-Size` or query params) |
| `GET` | `/library/metadata/{ratingKey}/grandchildren` | Show → all episodes (flattened), paged; season → same as children |
| `POST` | `/library/metadata/matches` | Match show (2), season (3), or episode (4) from JSON body |

`/library/metadata/{id}/images` is not implemented yet (posters could be wired from [`../images/`](../images/) later).

## Troubleshooting

- **503 from Worker:** KV key `catalog` missing or invalid JSON — re-run KV `put` after `build_catalog.py`.
- **Plex won’t register:** Check TLS, correct public URL, and that `GET /` returns valid `MediaProvider` JSON.
- **Catalog too large for KV:** Single key limit is about **25 MiB**; the workflow fails the build if the file exceeds a safe threshold. If you outgrow that, shard by season and extend the Worker.
