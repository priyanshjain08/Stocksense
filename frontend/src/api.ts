const BASE = import.meta.env.VITE_API_URL ?? "http://localhost:8000";
const API = `${BASE}/api/v1`;

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

export const session = {
  get token() { return sessionStorage.getItem("token"); },
  set token(v: string | null) { v ? sessionStorage.setItem("token", v) : sessionStorage.removeItem("token"); },
  get org() { return sessionStorage.getItem("org"); },
  set org(v: string | null) { v ? sessionStorage.setItem("org", v) : sessionStorage.removeItem("org"); },
};

export async function api<T = any>(path: string, opts: { method?: string; body?: unknown; headers?: Record<string, string> } = {}): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...(opts.headers ?? {}) };
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  if (session.org) headers["X-Organization-Id"] = session.org;
  const res = await fetch(API + path, { method: opts.method ?? (opts.body ? "POST" : "GET"), headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new ApiError(res.status, data?.error?.code ?? "error", data?.error?.message ?? res.statusText);
  return data as T;
}

export const newKey = () => crypto.randomUUID();

/** Download a binary file (e.g. xlsx) with the auth headers attached. */
export async function download(path: string, filename: string): Promise<void> {
  const headers: Record<string, string> = {};
  if (session.token) headers.Authorization = `Bearer ${session.token}`;
  if (session.org) headers["X-Organization-Id"] = session.org;
  const res = await fetch(API + path, { headers });
  if (!res.ok) throw new ApiError(res.status, "download_failed", "Could not download the report");
  const url = URL.createObjectURL(await res.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  a.click();
  URL.revokeObjectURL(url);
}
