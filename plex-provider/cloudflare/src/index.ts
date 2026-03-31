/**
 * Plex custom metadata provider Worker.
 * @see https://developer.plex.tv/pms/index.html#section/API-Info/Metadata-Providers
 */

export interface Env {
  CATALOG: KVNamespace;
  PROVIDER_TITLE: string;
}

type PlexType = "show" | "season" | "episode";

interface Item {
  type: PlexType;
  ratingKey: string;
  key: string;
  guid: string;
  title: string;
  summary?: string;
  originallyAvailableAt: string;
  year?: number;
  index?: number;
  parentIndex?: number;
  parentRatingKey?: string;
  parentKey?: string;
  parentGuid?: string;
  parentType?: string;
  parentTitle?: string;
  grandparentRatingKey?: string;
  grandparentKey?: string;
  grandparentGuid?: string;
  grandparentType?: string;
  grandparentTitle?: string;
}

interface Catalog {
  catalogVersion: number;
  generatedAt: string;
  identifier: string;
  showRatingKey: string;
  items: Record<string, Item>;
  children: Record<string, string[]>;
}

const JSON_HDR = { "Content-Type": "application/json; charset=utf-8" };

const cors = (req: Request, base: Record<string, string> = {}): Record<string, string> => {
  const o = req.headers.get("Origin");
  const h: Record<string, string> = { ...base };
  if (o) {
    h["Access-Control-Allow-Origin"] = o;
    h["Vary"] = "Origin";
  } else {
    h["Access-Control-Allow-Origin"] = "*";
  }
  h["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS";
  h["Access-Control-Allow-Headers"] = "Content-Type, X-Plex-Container-Start, X-Plex-Container-Size";
  return h;
};

let catalogCache: { at: number; data: Catalog } | null = null;
const CACHE_MS = 60_000;

async function getCatalog(env: Env): Promise<Catalog | null> {
  const now = Date.now();
  if (catalogCache && now - catalogCache.at < CACHE_MS) {
    return catalogCache.data;
  }
  const raw = await env.CATALOG.get("catalog");
  if (!raw) return null;
  try {
    const data = JSON.parse(raw) as Catalog;
    catalogCache = { at: now, data };
    return data;
  } catch {
    return null;
  }
}

function yearFromDate(iso: string | undefined): number | undefined {
  if (!iso || iso.length < 4) return undefined;
  const y = parseInt(iso.slice(0, 4), 10);
  return Number.isFinite(y) ? y : undefined;
}

function cloneItem(i: Item): Record<string, unknown> {
  const o: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(i)) {
    if (v !== undefined && v !== null) o[k] = v;
  }
  return o;
}

function mediaContainer(
  identifier: string,
  metadata: Record<string, unknown>[],
  offset = 0
): Record<string, unknown> {
  return {
    MediaContainer: {
      offset,
      totalSize: metadata.length,
      identifier,
      size: metadata.length,
      Metadata: metadata,
    },
  };
}

function parsePaging(req: Request): { start: number; size: number } {
  const url = new URL(req.url);
  const gh = (name: string) =>
    req.headers.get(name) || url.searchParams.get(name) || "";
  let start = parseInt(gh("X-Plex-Container-Start"), 10);
  let size = parseInt(gh("X-Plex-Container-Size"), 10);
  if (!Number.isFinite(start) || start < 0) start = 0;
  if (!Number.isFinite(size) || size < 1) size = 20;
  if (size > 500) size = 500;
  return { start, size };
}

function sliceKeys(keys: string[], start: number, size: number): string[] {
  return keys.slice(start, start + size);
}

function childrenObject(
  catalog: Catalog,
  childKeys: string[],
  simplified: boolean
): { size: number; Metadata: Record<string, unknown>[] } {
  const md: Record<string, unknown>[] = [];
  for (const rk of childKeys) {
    const it = catalog.items[rk];
    if (!it) continue;
      if (simplified) {
      const y = yearFromDate(it.originallyAvailableAt);
      const row: Record<string, unknown> = {
        guid: it.guid,
        type: it.type,
        title: it.title,
        ratingKey: it.ratingKey,
        key: it.key,
        index: it.index,
        originallyAvailableAt: it.originallyAvailableAt,
      };
      if (y !== undefined) row.year = y;
      if (it.parentTitle) row.parentTitle = it.parentTitle;
      if (it.parentGuid) row.parentGuid = it.parentGuid;
      md.push(row);
    } else {
      md.push(cloneItem(it));
    }
  }
  return { size: md.length, Metadata: md };
}

function attachChildrenIfRequested(
  catalog: Catalog,
  item: Item,
  includeChildren: boolean
): Record<string, unknown> {
  const base = cloneItem(item);
  if (!includeChildren) return base;
  if (item.type === "show" || item.type === "season") {
    const keys = catalog.children[item.ratingKey] || [];
    base.Children = childrenObject(catalog, keys, false);
  }
  return base;
}

function grandchildrenKeys(catalog: Catalog, ratingKey: string): string[] {
  const item = catalog.items[ratingKey];
  if (!item) return [];
  if (item.type === "season") {
    return catalog.children[ratingKey] || [];
  }
  if (item.type === "show") {
    const seasonKeys = catalog.children[ratingKey] || [];
    const out: string[] = [];
    for (const sk of seasonKeys) {
      const eps = catalog.children[sk] || [];
      out.push(...eps);
    }
    return out;
  }
  return [];
}

function mediaProviderJson(catalog: Catalog, title: string): Record<string, unknown> {
  const id = catalog.identifier;
  return {
    MediaProvider: {
      identifier: id,
      title,
      version: "1.0.0",
      Types: [
        { type: 2, Scheme: [{ scheme: id }] },
        { type: 3, Scheme: [{ scheme: id }] },
        { type: 4, Scheme: [{ scheme: id }] },
      ],
      Feature: [
        { type: "metadata", key: "/library/metadata" },
        { type: "match", key: "/library/metadata/matches" },
      ],
    },
  };
}

interface MatchBody {
  type?: number;
  title?: string;
  parentTitle?: string;
  grandparentTitle?: string;
  parentIndex?: number;
  index?: number;
  filename?: string;
  guid?: string;
  manual?: number;
  year?: number;
  date?: string;
}

const SXXEYY = /S(\d{1,3})E(\d{1,4})/i;

function parseSeasonEpisodeFromFilename(filename: string): { s: number; e: number } | null {
  const m = filename.match(SXXEYY);
  if (!m) return null;
  return { s: parseInt(m[1], 10), e: parseInt(m[2], 10) };
}

function findEpisodesByNumbers(
  catalog: Catalog,
  season: number,
  episode: number
): Item[] {
  return Object.values(catalog.items).filter(
    (i) =>
      i.type === "episode" &&
      i.parentIndex === season &&
      i.index === episode
  );
}

function rankEpisodeKeys(items: Item[]): Item[] {
  const copy = [...items];
  copy.sort((a, b) => {
    const ae = a.ratingKey.includes("-extended") ? 1 : 0;
    const be = b.ratingKey.includes("-extended") ? 1 : 0;
    if (ae !== be) return ae - be;
    return a.ratingKey.localeCompare(b.ratingKey);
  });
  return copy;
}

function matchMetadata(
  catalog: Catalog,
  body: MatchBody
): Record<string, unknown>[] {
  const t = body.type ?? 0;
  const manual = body.manual === 1;

  if (t === 2) {
    const show = catalog.items[catalog.showRatingKey];
    if (!show) return [];
    const title = (body.title || "").toLowerCase();
    const ok =
      !title ||
      show.title.toLowerCase().includes(title) ||
      title.includes("one pace") ||
      title.includes("onepace");
    if (!ok) return [];
    return manual ? [cloneItem(show)] : [cloneItem(show)];
  }

  if (t === 3) {
    const idx = body.index;
    if (typeof idx !== "number" || idx < 0) return [];
    const sk = `season-${idx}`;
    const season = catalog.items[sk];
    if (!season || season.type !== "season") return [];
    return [cloneItem(season)];
  }

  if (t === 4) {
    let season = typeof body.parentIndex === "number" ? body.parentIndex : -1;
    let episode = typeof body.index === "number" ? body.index : -1;
    if (typeof body.filename === "string") {
      const pe = parseSeasonEpisodeFromFilename(body.filename);
      if (pe) {
        if (season < 0) season = pe.s;
        if (episode < 0) episode = pe.e;
      }
    }
    if (season < 0 || episode < 0) return [];
    let eps = findEpisodesByNumbers(catalog, season, episode);
    eps = rankEpisodeKeys(eps);
    if (eps.length === 0) return [];
    if (manual) {
      return eps.map((e) => cloneItem(e));
    }
    return [cloneItem(eps[0])];
  }

  return [];
}

export default {
  async fetch(req: Request, env: Env, _ctx: ExecutionContext): Promise<Response> {
    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: cors(req) });
    }

    const catalog = await getCatalog(env);
    if (!catalog) {
      return new Response(JSON.stringify({ error: "catalog missing; upload KV key catalog" }), {
        status: 503,
        headers: cors(req, JSON_HDR),
      });
    }

    const url = new URL(req.url);
    const path = url.pathname.replace(/\/$/, "") || "/";

    if (path === "/" && req.method === "GET") {
      const title = env.PROVIDER_TITLE || "One Pace";
      return new Response(JSON.stringify(mediaProviderJson(catalog, title)), {
        headers: cors(req, JSON_HDR),
      });
    }

    if (path === "/library/metadata/matches" && req.method === "POST") {
      let body: MatchBody = {};
      try {
        body = (await req.json()) as MatchBody;
      } catch {
        return new Response(JSON.stringify({ error: "invalid JSON" }), {
          status: 400,
          headers: cors(req, JSON_HDR),
        });
      }
      const matches = matchMetadata(catalog, body);
      const mc = mediaContainer(catalog.identifier, matches);
      return new Response(JSON.stringify(mc), { headers: cors(req, JSON_HDR) });
    }

    const childRe = /^\/library\/metadata\/([^/]+)\/(children|grandchildren)$/;
    const childM = path.match(childRe);
    if (childM && req.method === "GET") {
      const id = childM[1];
      const which = childM[2];
      const { start, size } = parsePaging(req);
      let keys: string[] =
        which === "children"
          ? catalog.children[id] || []
          : grandchildrenKeys(catalog, id);
      const total = keys.length;
      keys = sliceKeys(keys, start, size);
      const md = keys
        .map((rk) => catalog.items[rk])
        .filter(Boolean)
        .map((it) => cloneItem(it!));
      return new Response(
        JSON.stringify({
          MediaContainer: {
            offset: start,
            totalSize: total,
            identifier: catalog.identifier,
            size: md.length,
            Metadata: md,
          },
        }),
        { headers: cors(req, JSON_HDR) }
      );
    }

    const metaRe = /^\/library\/metadata\/([^/]+)$/;
    const metaM = path.match(metaRe);
    if (metaM && req.method === "GET") {
      const id = metaM[1];
      const item = catalog.items[id];
      if (!item) {
        return new Response(JSON.stringify({ error: "not found" }), {
          status: 404,
          headers: cors(req, JSON_HDR),
        });
      }
      const includeChildren =
        url.searchParams.get("includeChildren") === "1" ||
        url.searchParams.get("includeChildren") === "true";
      const md = attachChildrenIfRequested(catalog, item, includeChildren);
      return new Response(
        JSON.stringify(mediaContainer(catalog.identifier, [md])),
        { headers: cors(req, JSON_HDR) }
      );
    }

    return new Response(JSON.stringify({ error: "not found" }), {
      status: 404,
      headers: cors(req, JSON_HDR),
    });
  },
};
