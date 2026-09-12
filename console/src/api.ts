export type Row = { id: string; [key: string]: unknown };
export type Page = { data: Row[]; next_cursor: string | null; as_of: string };
export type Bootstrap = {
  deployment_id: string;
  version: string;
  ready: boolean;
  applications: {
    id: string;
    tenant_mode: 'single' | 'multi';
    enabled: boolean;
    configured: boolean;
  }[];
  sessions: { default_ttl_seconds: number; max_ttl_seconds: number };
  integrations: {
    app_id: string;
    id: string;
    connector: string;
    endpoint: string;
    enabled: boolean;
    oauth_configured: boolean;
    oauth_profile: string | null;
    callback_url: string | null;
    reviewed_tools: string[];
    schema_hashes: Record<string, string>;
  }[];
};
let csrf = '';
export function setCsrf(value: string) {
  csrf = value;
}
export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch('/console/api/v1' + path, {
    ...init,
    credentials: 'same-origin',
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      ...(csrf ? { 'X-CSRF-Token': csrf } : {}),
      ...init.headers,
    },
  });
  const data = await response.json();
  if (!response.ok)
    throw new ApiError(
      response.status,
      data.error?.message || 'The server could not complete this request.',
    );
  return data;
}
export function query(values: Record<string, string | undefined>) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(values))
    if (value !== undefined && value !== '') params.set(key, value);
  return '?' + params.toString();
}
export function text(row: Row, key: string): string {
  return row[key] == null ? '—' : String(row[key]);
}
export function rowKey(row: Row): string {
  return JSON.stringify([row.app_id, row.tenant_id, row.id]);
}
export function date(value: unknown): string {
  if (!value) return '—';
  const time = new Date(String(value));
  return Number.isNaN(time.getTime()) ? '—' : time.toLocaleString();
}
