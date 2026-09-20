import { describe, expect, it, vi, afterEach } from 'vitest';
import { api, ApiError, date, permissionBody, permissionsOf, query, rowKey, setCsrf } from './api';

afterEach(() => {
  vi.unstubAllGlobals();
  setCsrf('');
});
describe('console API client', () => {
  it('keeps actor identifiers separate and encodes query values', () => {
    const params = new URLSearchParams(
      query({
        app_id: 'demo',
        tenant_id: 'tenant/a',
        user_id: 'alice+bob@example.com',
        status: '',
      }),
    );
    expect(params.get('tenant_id')).toBe('tenant/a');
    expect(params.get('user_id')).toBe('alice+bob@example.com');
    expect(params.has('status')).toBe(false);
  });
  it('uses namespace-aware row identities', () => {
    expect(rowKey({ id: 'same', app_id: 'a', tenant_id: 'one' })).not.toBe(
      rowKey({ id: 'same', app_id: 'a', tenant_id: 'two' }),
    );
  });
  it('uses cookies and CSRF without an application credential', async () => {
    const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ committed: true }) });
    vi.stubGlobal('fetch', fetch);
    setCsrf('csrf-nonce');
    await api('/sessions/id/revoke', { method: 'POST', body: '{}' });
    expect(fetch.mock.calls[0][0]).toBe('/console/api/v1/sessions/id/revoke');
    expect(fetch.mock.calls[0][1]).toMatchObject({
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'X-CSRF-Token': 'csrf-nonce' },
    });
    expect(fetch.mock.calls[0][1].headers.Authorization).toBeUndefined();
  });
  it('does not turn server errors into successes', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 412,
        json: async () => ({ error: { message: 'Connection changed.' } }),
      }),
    );
    await expect(api('/connections/id/disconnect', { method: 'POST' })).rejects.toBeInstanceOf(
      ApiError,
    );
  });
  it('does not display invalid timestamps', () => {
    expect(date(null)).toBe('—');
    expect(date('not a date')).toBe('—');
  });
});
describe('session permissions', () => {
  const row = {
    id: 'sess_1',
    app_id: 'demo',
    tenant_id: 'default',
    permissions: [
      { name: 'issue_read', allowed: true, effect: 'read' },
      { name: 'list_issues', allowed: false, effect: 'read' },
    ],
  };
  it('sends one tool per change, inside the row namespace', () => {
    expect(permissionBody(row, 'list_issues', true)).toEqual({
      app_id: 'demo',
      tenant_id: 'default',
      tools: { list_issues: true },
    });
  });
  it('reads decisions from a detail row and tolerates an older payload', () => {
    expect(permissionsOf(row).map((p) => p.allowed)).toEqual([true, false]);
    expect(permissionsOf({ id: 'sess_2' })).toEqual([]);
    expect(permissionsOf({ id: 'sess_3', permissions: null })).toEqual([]);
  });
  it('never infers a namespace the row does not carry', () => {
    const body = permissionBody({ id: 'sess_4' }, 'issue_read', false);
    expect(body.app_id).toBe('');
    expect(body.tenant_id).toBe('');
  });
});
