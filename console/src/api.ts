export type Row = { id: string; [key: string]: unknown };
export type Page = { data: Row[]; next_cursor: string | null; as_of: string };
/** One tool a session may be granted, with the operator's present decision. */
export type Permission = { name: string; allowed: boolean; effect: 'read' | 'write' };
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
    tools: { name: string; effect: 'read' | 'write' }[];
    schema_hashes: Record<string, string>;
  }[];
  features: {
    oauth: boolean;
    writes: boolean;
    scope_editing: boolean;
    session_permissions: boolean;
    live_provider_verified: boolean;
  };
};
/** Reads the permission rows a session detail carries, tolerating an older payload. */
export function permissionsOf(row: Row): Permission[] {
  return Array.isArray(row.permissions) ? (row.permissions as Permission[]) : [];
}
/** The request body for one toggle: the server accepts only ceiling names. */
export function permissionBody(row: Row, name: string, allowed: boolean) {
  return {
    app_id: String(row.app_id ?? ''),
    tenant_id: String(row.tenant_id ?? ''),
    tools: { [name]: allowed },
  };
}
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
