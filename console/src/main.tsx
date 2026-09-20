import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import originalComponents from './reference/components.js';
import './reference/tokens.css';
import './console.css';
import {
  api,
  ApiError,
  Bootstrap,
  date,
  Page,
  Permission,
  permissionBody,
  permissionsOf,
  query,
  Row,
  rowKey,
  setCsrf,
  text,
} from './api';

// These are the actual primitives extracted from the user's Ravn Console.html.
const {
  Button,
  NavRail,
  TopBar,
  DataTable,
  Drawer,
  ConfirmModal,
  Toast,
  StatusBadge,
  EmptyState,
  AgentGraph,
  DataValue,
  Switch,
} = originalComponents as unknown as Record<string, React.ComponentType<any>>;

type View = 'connections' | 'sessions' | 'permissions' | 'activity' | 'settings';
type Selection = { kind: string; row: Row };
const NAV = [
  {
    items: [
      { id: 'connections', label: 'Connections', icon: 'database' },
      { id: 'sessions', label: 'Sessions', icon: 'key-round' },
      { id: 'permissions', label: 'Permissions', icon: 'shield' },
      { id: 'activity', label: 'Activity', icon: 'activity' },
    ],
  },
  { label: 'Admin', items: [{ id: 'settings', label: 'Settings', icon: 'settings' }] },
];
const TITLES: Record<View, string> = {
  connections: 'Connections',
  sessions: 'Sessions',
  permissions: 'Permissions',
  activity: 'Activity',
  settings: 'Settings',
};
const ICONS: Record<View, string> = {
  connections: 'database',
  sessions: 'key-round',
  permissions: 'shield',
  activity: 'activity',
  settings: 'settings',
};
const PROVIDERS: Record<string, string> = {
  github_cloud: 'GitHub',
  slack: 'Slack',
  gmail: 'Gmail',
};

function State({ value }: { value: unknown }) {
  const label = String(value || 'unknown');
  const status = ['active', 'enabled', 'succeeded'].includes(label)
    ? 'ok'
    : ['failed', 'tool_error'].includes(label)
      ? 'risk'
      : ['blocked', 'unknown', 'running', 'reconnect_required'].includes(label)
        ? 'caution'
        : 'neutral';
  return <StatusBadge status={status}>{label.replaceAll('_', ' ')}</StatusBadge>;
}
function Facts({ items }: { items: [string, unknown][] }) {
  return (
    <dl className="facts">
      {items.map(([label, value]) => (
        <React.Fragment key={label}>
          <dt>{label}</dt>
          <dd>{String(value ?? '—')}</dd>
        </React.Fragment>
      ))}
    </dl>
  );
}
function Choice({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <label className="choice">
      <span>{label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        {options.map((o) => (
          <option value={o.value} key={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </label>
  );
}
function FocusScope({
  children,
  onClose,
  label,
  modal = false,
}: {
  children: React.ReactNode;
  onClose: () => void;
  label: string;
  modal?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const el = ref.current;
    el?.querySelector<HTMLElement>('button, [tabindex="0"]')?.focus();
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.stopPropagation();
        close.current();
      }
      if (event.key !== 'Tab' || !modal || !el) return;
      const nodes = [
        ...el.querySelectorAll<HTMLElement>('button:not(:disabled), input, select, [tabindex="0"]'),
      ];
      const first = nodes[0],
        last = nodes.at(-1);
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    el?.addEventListener('keydown', key);
    return () => {
      el?.removeEventListener('keydown', key);
      if (previous?.isConnected) previous.focus();
    };
  }, [modal]);
  return (
    <div
      ref={ref}
      className="focus-scope"
      role={modal ? 'alertdialog' : 'dialog'}
      aria-modal={modal || undefined}
      aria-label={label}
    >
      {children}
    </div>
  );
}

function Detail({
  selection,
  onClose,
  onAct,
  onSessions,
  onCalls,
  onPermission,
}: {
  selection: Selection;
  onClose: () => void;
  onAct: (s: Selection) => void;
  onSessions: (id: string) => void;
  onCalls: (field: string, id: string) => void;
  onPermission: (row: Row, name: string, allowed: boolean) => Promise<Permission[] | null>;
}) {
  const [row, setRow] = useState(selection.row);
  const [busy, setBusy] = useState('');
  // A ref, not the state above: two clicks in one tick both read the old state.
  const toggling = useRef(false);
  const [refresh, setRefresh] = useState(0);
  const [error, setError] = useState('');
  const [loaded, setLoaded] = useState(false);
  const [related, setRelated] = useState<Row[]>([]);
  const [map, setMap] = useState(false);
  const [mapSelected, setMapSelected] = useState<string | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    setError('');
    setLoaded(false);
    if (['events', 'app-keys'].includes(selection.kind)) {
      setLoaded(true);
      return;
    }
    api<Row>(
      `/${selection.kind}/${encodeURIComponent(selection.row.id)}` +
        query({
          app_id: text(selection.row, 'app_id'),
          tenant_id: text(selection.row, 'tenant_id'),
        }),
      { signal: abort.signal },
    )
      .then((data) => {
        setRow(data);
        setLoaded(true);
      })
      .catch((e) => {
        if (e.name !== 'AbortError') setError(e.message);
      });
    return () => abort.abort();
  }, [selection, refresh]);
  useEffect(() => {
    if (!map) return;
    const abort = new AbortController();
    api<Page>(
      '/sessions' +
        query({
          app_id: text(row, 'app_id'),
          tenant_id: text(row, 'tenant_id'),
          connection_id: row.id,
          limit: '10',
        }),
      { signal: abort.signal },
    )
      .then((data) => setRelated(data.data))
      .catch((e) => {
        if (e.name !== 'AbortError') setError(e.message);
      });
    return () => abort.abort();
  }, [map, row]);
  const kind = selection.kind;
  const canAct =
    loaded &&
    !error &&
    (kind === 'connections'
      ? row.status !== 'disconnected'
      : kind === 'sessions'
        ? !row.revoked_at
        : kind === 'app-keys');
  const title =
    kind === 'connections'
      ? text(row, 'display_name')
      : kind === 'calls'
        ? text(row, 'tool')
        : kind === 'events'
          ? text(row, 'kind').replaceAll('.', ' · ')
          : kind === 'app-keys'
            ? text(row, 'label')
            : 'Runtime session';
  return (
    <FocusScope label={title} onClose={onClose}>
      <Drawer
        open
        title={title}
        subtitle={<code>{row.id}</code>}
        onClose={onClose}
        width="min(520px, 100vw)"
        footer={
          <>
            {canAct && (
              <Button variant="destructive" onClick={() => onAct({ kind, row })}>
                {kind === 'connections'
                  ? 'Disconnect'
                  : kind === 'app-keys'
                    ? 'Revoke key'
                    : 'Revoke session'}
              </Button>
            )}
            <Button variant="secondary" onClick={onClose}>
              Close
            </Button>
          </>
        }
      >
        <div className="detail-content">
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}
          {!loaded && !error && <p role="status">Loading current state…</p>}
          <Facts
            items={[
              ['Application', row.app_id],
              ['Tenant', row.tenant_id],
              ['User', row.user_id],
              ['Created', date(row.created_at)],
            ]}
          />
          {kind === 'connections' && (
            <>
              <h2>Connection</h2>
              <Facts
                items={[
                  ['Integration', row.integration_id],
                  ['Provider account', row.provider_account_id],
                  [
                    'Authentication',
                    row.credential_type === 'oauth2'
                      ? 'OAuth · managed refresh when issued'
                      : 'Imported user token',
                  ],
                  ['Saved state', row.status],
                  ['Broker access', row.broker_access],
                  ['Credential version', row.credential_version],
                  ['Revision', row.revision],
                ]}
              />
              <p className="muted">
                Saved credentials do not prove current provider access. Provider credentials are
                never displayed.
              </p>
              {row.status === 'reconnect_required' && (
                <p className="muted">
                  Authorize the same account again through your backend, or use{' '}
                  <code>
                    ravn connections connect --integration {text(row, 'integration_id')} --reconnect{' '}
                    {row.id} --app-key-file … --user …
                  </code>
                  . Existing runtime sessions will need replacement.
                </p>
              )}
              <div className="button-row">
                <Button variant="secondary" onClick={() => onSessions(row.id)}>
                  View sessions
                </Button>
                <Button variant="secondary" onClick={() => onCalls('connection_id', row.id)}>
                  View calls
                </Button>
                <Button variant="secondary" onClick={() => setMap((v) => !v)}>
                  Access map
                </Button>
              </div>
              {map && (
                <section>
                  <h2>Access map</h2>
                  <div className="access-map">
                    <AgentGraph
                      showColumnLabels={false}
                      height={280}
                      selectedId={mapSelected}
                      onSelectNode={(node: { id: string }) => setMapSelected(node.id)}
                      nodes={[
                        {
                          id: 'owner',
                          label: text(row, 'user_id'),
                          kind: 'root',
                          permissions: 'Connection owner',
                          icon: 'user',
                        },
                        ...related.map((s) => ({
                          id: s.id,
                          label: s.id.slice(0, 17) + '…',
                          permissions: text(s, 'status'),
                          kind: 'agent',
                          status: s.broker_access === 'enabled' ? 'ok' : 'neutral',
                        })),
                        {
                          id: row.id,
                          label: text(row, 'display_name'),
                          kind: 'resource',
                          system: text(row, 'integration_id') + ' connection',
                        },
                      ]}
                      edges={[
                        { from: 'owner', to: row.id, label: 'owns' },
                        ...related.map((s) => ({
                          from: s.id,
                          to: row.id,
                          label: 'bound to',
                          kind: s.broker_access === 'enabled' ? 'grant' : 'inactive',
                        })),
                      ]}
                    />
                  </div>
                  <p className="muted">
                    Showing up to 10 sessions. These are ownership and connection bindings—not
                    delegation. Use View sessions for the full list.
                  </p>
                </section>
              )}
            </>
          )}
          {kind === 'sessions' && (
            <>
              <h2>Runtime access</h2>
              <Facts
                items={[
                  ['Connection', row.connection_id],
                  ['Token state', row.status],
                  ['Broker access', row.broker_access],
                  ['Blocked reason', row.blocked_reason],
                  ['Expires', date(row.expires_at)],
                  ['Revoked', date(row.revoked_at)],
                ]}
              />
              <h2>Tools this session may call</h2>
              {permissionsOf(row).length ? (
                <div className="permission-list">
                  {permissionsOf(row).map((permission: Permission) => (
                    <Switch
                      key={permission.name}
                      checked={permission.allowed}
                      disabled={busy !== '' || row.status !== 'active'}
                      label={permission.name}
                      hint={
                        permission.effect === 'write'
                          ? 'Changes the provider. Never retried.'
                          : 'Read only.'
                      }
                      onChange={async (next: boolean) => {
                        if (toggling.current) return;
                        toggling.current = true;
                        setBusy(permission.name);
                        try {
                          const updated = await onPermission(row, permission.name, next);
                          if (updated) setRow((old) => ({ ...old, permissions: updated }));
                        } finally {
                          toggling.current = false;
                          setBusy('');
                          // Re-read the row: a change also moves current_tools and
                          // blocked_reason, which the response above does not carry.
                          setRefresh((v) => v + 1);
                        }
                      }}
                    />
                  ))}
                </div>
              ) : (
                <p className="muted">
                  No reviewed tools remain for this session. Pin schemas for the integration and
                  issue a new session.
                </p>
              )}
              <p className="muted">
                {row.status === 'active'
                  ? 'A change takes effect on the next call, including for an agent already running. Turning a tool off never ends the session.'
                  : 'This session is no longer usable, so these settings do not grant anything.'}{' '}
                The session ceiling is {Array.isArray(row.tools) ? row.tools.join(', ') : 'empty'};
                nothing outside it can be granted here. The provider's own account and token
                permissions apply independently and are not enumerated.
              </p>
              <Button variant="secondary" onClick={() => onCalls('session_id', row.id)}>
                View calls
              </Button>
            </>
          )}
          {kind === 'calls' && (
            <>
              <h2>Recorded outcome</h2>
              <State value={row.status} />
              {row.effect === 'write' && row.status === 'unknown' && (
                <p className="muted">
                  This write may have run at the provider. RAVN never retries it; check the provider
                  before trying again.
                </p>
              )}
              <Facts
                items={[
                  ['Tool', row.tool],
                  ['Effect', row.effect === 'write' ? 'Write (never retried)' : 'Read'],
                  ['Connection', row.connection_id],
                  ['Session', row.session_id],
                  ['Duration', row.duration_ms == null ? '—' : row.duration_ms + ' ms'],
                  ['Error code', row.error_code],
                  ['Completed', date(row.completed_at)],
                  ['Argument fingerprint', row.arguments_fingerprint],
                ]}
              />
              <p className="muted">Metadata only. Raw arguments and results are not retained.</p>
            </>
          )}
          {kind === 'events' && (
            <Facts
              items={[
                ['Event', row.kind],
                ['Actor category', row.actor_kind],
                ['Actor ID', row.actor_id],
                ['Subject', row.subject_id],
                ['Request ID', row.request_id],
              ]}
            />
          )}
          {kind === 'app-keys' && (
            <>
              <Facts
                items={[
                  ['Key state', row.revoked_at ? 'revoked' : 'active'],
                  ['Revoked', date(row.revoked_at)],
                ]}
              />
              <p className="muted">
                Only metadata is available. Create a replacement through the owner-only CLI.
              </p>
              <code className="command">
                ravn app-key create --app {text(row, 'app_id')} --output /private/path/backend.key
              </code>
            </>
          )}
        </div>
      </Drawer>
    </FocusScope>
  );
}

function ConsoleApp() {
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [loginError, setLoginError] = useState('');
  const [starting, setStarting] = useState(true);
  const [app, setApp] = useState('');
  const [view, setView] = useState<View>('connections');
  const [tab, setTab] = useState('calls');
  const [settings, setSettings] = useState('integrations');
  const [sidebar, setSidebar] = useState(true);
  const [user, setUser] = useState(''),
    [tenant, setTenant] = useState(''),
    [status, setStatus] = useState('');
  const [filters, setFilters] = useState<Record<string, string>>({});
  const [relatedFilter, setRelatedFilter] = useState<Record<string, string>>({});
  const [records, setRecords] = useState<Row[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [pageCount, setPageCount] = useState(1);
  const [asOf, setAsOf] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [confirm, setConfirm] = useState<Selection | null>(null);
  const [compromise, setCompromise] = useState(false);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState('');
  const [toastError, setToastError] = useState(false);
  const previousQuery = useRef('');
  const seq = useRef(0),
    moreAbort = useRef<AbortController | null>(null),
    busyRef = useRef(false);

  useEffect(() => {
    const fragment = new URLSearchParams(location.hash.slice(1));
    const ticket = fragment.get('ticket');
    if (location.hash) history.replaceState(null, '', location.pathname);
    (async () => {
      const session = await api<{ csrf_token: string }>(
        ticket ? '/auth/exchange' : '/auth/session',
        ticket ? { method: 'POST', body: JSON.stringify({ ticket }) } : {},
      );
      setCsrf(session.csrf_token);
      const data = await api<Bootstrap>('/bootstrap');
      setBoot(data);
      setApp(data.applications[0]?.id || '');
    })()
      .catch((e) => setLoginError(e.message))
      .finally(() => setStarting(false));
  }, []);
  const multi = boot?.applications.find((a) => a.id === app)?.tenant_mode === 'multi';
  const resource =
    view === 'activity'
      ? tab
      : view === 'settings'
        ? settings === 'keys'
          ? 'app-keys'
          : ''
        : // Permissions are granted per session, so it reads the same records.
          view === 'permissions'
          ? 'sessions'
          : view;
  const params = useMemo(
    () => ({
      app_id: app,
      limit: '50',
      ...(view === 'settings' ? {} : filters),
      ...(['calls', 'sessions'].includes(resource) ? relatedFilter : {}),
    }),
    [app, view, filters, relatedFilter, resource],
  );
  const requestKey = JSON.stringify([resource, params]);
  const handleError = useCallback((e: Error) => {
    if (e.name === 'AbortError') return;
    if (e instanceof ApiError && e.status === 401) {
      setBoot(null);
      setCsrf('');
      setLoginError(e.message);
    } else setError(e.message);
  }, []);
  useEffect(() => {
    moreAbort.current?.abort();
    const serial = ++seq.current,
      abort = new AbortController();
    if (previousQuery.current !== requestKey) setRecords([]);
    previousQuery.current = requestKey;
    setNext(null);
    setPageCount(1);
    setError('');
    if (!boot || !app || !resource) {
      setLoading(false);
      return;
    }
    setLoading(true);
    api<Page>('/' + resource + query(params), { signal: abort.signal })
      .then((page) => {
        if (serial !== seq.current) return;
        setRecords(page.data);
        setNext(page.next_cursor);
        setAsOf(page.as_of);
      })
      .catch(handleError)
      .finally(() => {
        if (serial === seq.current) setLoading(false);
      });
    return () => abort.abort();
  }, [requestKey, reload, boot, handleError]);
  useEffect(() => {
    if (view !== 'activity' || pageCount > 1 || selection || confirm) return;
    const timer = setInterval(() => {
      if (!document.hidden) setReload((v) => v + 1);
    }, 5000);
    return () => clearInterval(timer);
  }, [view, pageCount, selection, confirm]);
  useEffect(() => {
    const toggle = (e: KeyboardEvent) => {
      if (e.key === '\\' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        setSidebar((v) => !v);
      }
    };
    window.addEventListener('keydown', toggle);
    return () => window.removeEventListener('keydown', toggle);
  }, []);
  function navigate(value: View) {
    setView(value);
    setSelection(null);
    setStatus('');
    setFilters({
      ...(user ? { user_id: user } : {}),
      ...(multi && tenant ? { tenant_id: tenant } : {}),
    });
    setRelatedFilter({});
  }
  function chooseApp(value: string) {
    setApp(value);
    setSelection(null);
    setFilters({});
    setRelatedFilter({});
    setUser('');
    setTenant('');
    setStatus('');
  }
  function linked(field: string, identity: string, target: View) {
    const row = selection?.row;
    setSelection(null);
    setView(target);
    setTab('calls');
    setStatus('');
    setFilters(row ? { tenant_id: text(row, 'tenant_id'), user_id: text(row, 'user_id') } : {});
    if (row) {
      setTenant(text(row, 'tenant_id'));
      setUser(text(row, 'user_id'));
    }
    setRelatedFilter({ [field]: identity });
  }
  async function more() {
    if (!next || loading) return;
    const serial = seq.current;
    moreAbort.current = new AbortController();
    setLoading(true);
    try {
      const page = await api<Page>('/' + resource + query({ ...params, cursor: next }), {
        signal: moreAbort.current.signal,
      });
      if (serial === seq.current) {
        setRecords((old) => [...old, ...page.data]);
        setNext(page.next_cursor);
        setPageCount((v) => v + 1);
      }
    } catch (e) {
      handleError(e as Error);
    } finally {
      if (serial === seq.current) setLoading(false);
    }
  }
  async function act() {
    if (!confirm || busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    const item = confirm;
    try {
      const body =
        item.kind === 'app-keys'
          ? { app_id: item.row.app_id, revoke_sessions: compromise }
          : { app_id: item.row.app_id, tenant_id: item.row.tenant_id };
      await api(
        `/${item.kind}/${encodeURIComponent(item.row.id)}/${item.kind === 'connections' ? 'disconnect' : 'revoke'}`,
        {
          method: 'POST',
          body: JSON.stringify(body),
          headers:
            item.kind === 'connections'
              ? { 'If-Match': '"' + String(item.row.revision) + '"' }
              : {},
        },
      );
      setToast(
        item.kind === 'connections'
          ? 'Connection disconnected. New calls are blocked.'
          : 'Access revocation committed.',
      );
      setToastError(false);
      setSelection(null);
      setConfirm(null);
      setReload((v) => v + 1);
    } catch (e) {
      setConfirm(null);
      setSelection(null);
      handleError(e as Error);
      setReload((v) => v + 1);
      setToast('Could not confirm the change. Refresh and inspect current state before retrying.');
      setToastError(true);
    } finally {
      setBusy(false);
      busyRef.current = false;
    }
  }
  async function changePermission(row: Row, name: string, allowed: boolean) {
    try {
      const result = await api<{ permissions: Permission[] }>(
        `/sessions/${encodeURIComponent(row.id)}/permissions`,
        { method: 'PUT', body: JSON.stringify(permissionBody(row, name, allowed)) },
      );
      setToast(
        allowed
          ? `${name} is allowed for this session.`
          : `${name} is denied. The next call is refused.`,
      );
      setToastError(false);
      setReload((v) => v + 1);
      return result.permissions;
    } catch (e) {
      handleError(e as Error);
      setReload((v) => v + 1);
      setToast('Could not change this permission. Refresh and check the session before retrying.');
      setToastError(true);
      return null;
    }
  }
  const open = (row: Row) => setSelection({ kind: resource, row });
  const identityColumn = {
    key: 'identity',
    label:
      resource === 'connections'
        ? 'Account'
        : resource === 'calls'
          ? 'Tool'
          : resource === 'events'
            ? 'Event'
            : resource === 'app-keys'
              ? 'Key'
              : 'Session',
    render: (r: Row) => (
      <button
        className="row-link"
        onClick={(e) => {
          e.stopPropagation();
          open(r);
        }}
      >
        <span>
          {resource === 'connections'
            ? text(r, 'display_name')
            : resource === 'calls'
              ? text(r, 'tool')
              : resource === 'events'
                ? text(r, 'kind')
                : resource === 'app-keys'
                  ? text(r, 'label')
                  : r.id.slice(0, 18) + '…'}
        </span>
        <code>{r.id}</code>
      </button>
    ),
  };
  const columns = [
    identityColumn,
    ...(resource !== 'app-keys'
      ? [
          { key: 'user_id', label: 'User', render: (r: Row) => text(r, 'user_id') },
          ...(multi ? [{ key: 'tenant_id', label: 'Tenant' }] : []),
        ]
      : []),
    ...(resource === 'events'
      ? [
          { key: 'actor_kind', label: 'Actor category' },
          { key: 'subject_id', label: 'Subject', mono: true },
        ]
      : [
          {
            key: 'status',
            label: 'State',
            render: (r: Row) => (
              <State
                value={resource === 'app-keys' ? (r.revoked_at ? 'revoked' : 'active') : r.status}
              />
            ),
          },
        ]),
    ...(['sessions', 'connections'].includes(resource)
      ? [
          {
            key: 'broker_access',
            label: 'Broker access',
            render: (r: Row) => <State value={r.broker_access} />,
          },
        ]
      : []),
    ...(view === 'permissions'
      ? [
          {
            key: 'permissions',
            label: 'Tools allowed',
            render: (r: Row) => {
              const items = permissionsOf(r);
              if (!items.length) return 'None';
              const allowed = items.filter((p) => p.allowed).length;
              return `${allowed} of ${items.length}`;
            },
          },
        ]
      : []),
    ...(resource === 'calls'
      ? [
          {
            key: 'effect',
            label: 'Effect',
            render: (r: Row) => (r.effect === 'write' ? 'Write' : 'Read'),
          },
          {
            key: 'duration_ms',
            label: 'Duration',
            render: (r: Row) => (r.duration_ms == null ? '—' : r.duration_ms + ' ms'),
          },
        ]
      : []),
    {
      key: resource === 'sessions' ? 'expires_at' : 'created_at',
      label: resource === 'sessions' ? 'Expires' : 'Created',
      render: (r: Row) => (
        <span title={text(r, resource === 'sessions' ? 'expires_at' : 'created_at')}>
          {date(r[resource === 'sessions' ? 'expires_at' : 'created_at'])}
        </span>
      ),
    },
  ].map((c) => ({ ...c, sortable: false }));

  if (!boot)
    return (
      <div className="login">
        <div className="login-card">
          <div className="wordmark">ravn</div>
          <h1>Operator console</h1>
          {starting ? (
            <p role="status">Opening your local console…</p>
          ) : (
            <>
              <p>This console is available to the owner of the RAVN server.</p>
              <p role="alert" className="muted">
                {loginError || 'Your session has ended.'}
              </p>
              <code className="command">ravn console</code>
              <p className="muted">
                Run the command on the server host to open a fresh sign-in link. Do not paste an
                application key here.
              </p>
              <Button variant="secondary" onClick={() => location.reload()}>
                Check session
              </Button>
            </>
          )}
        </div>
      </div>
    );

  const statuses =
    resource === 'connections'
      ? ['active', 'disconnected', 'reconnect_required']
      : resource === 'sessions'
        ? ['active', 'expired', 'revoked']
        : ['running', 'succeeded', 'tool_error', 'failed', 'unknown'];
  return (
    <div className="console-shell">
      {sidebar && (
        <div className="sidebar">
          <NavRail
            brand="ravn"
            groups={NAV}
            activeId={view}
            onSelect={navigate}
            onToggle={() => setSidebar(false)}
            user={{ name: 'Local operator', org: 'Deployment owner' }}
            action={
              <Button
                variant="secondary"
                style={{ width: '100%' }}
                onClick={async () => {
                  try {
                    await api('/auth/logout', { method: 'POST', body: '{}' });
                    setBoot(null);
                    setCsrf('');
                    setLoginError('Signed out.');
                  } catch (e) {
                    handleError(e as Error);
                  }
                }}
              >
                Sign out
              </Button>
            }
          />
        </div>
      )}
      <div className="workspace">
        <TopBar
          icon={ICONS[view]}
          title={TITLES[view]}
          onShowSidebar={sidebar ? undefined : () => setSidebar(true)}
          right={
            <>
              <Choice
                label="App"
                value={app}
                options={boot.applications.map((a) => ({
                  value: a.id,
                  label: a.id + (a.enabled ? '' : ' · disabled'),
                }))}
                onChange={chooseApp}
              />
              <span className="deployment" title="This deployment">
                {boot.deployment_id}
              </span>
            </>
          }
        />
        <main>
          <div className="page-header">
            <div>
              <h1>{TITLES[view]}</h1>
              <p className="muted">
                {view === 'connections'
                  ? 'User accounts available through RAVN.'
                  : view === 'sessions'
                    ? 'Short-lived access to one connection per session.'
                    : view === 'permissions'
                      ? 'Choose the tools each session may call. Changes apply to the running agent at its next call.'
                      : view === 'activity'
                        ? 'Recorded calls and management events. No arguments or results.'
                        : 'Operator-owned configuration. Changes remain in the CLI and config file.'}
              </p>
            </div>
            <Button
              variant="secondary"
              onClick={() => {
                setSelection(null);
                setReload((v) => v + 1);
                if (view === 'settings')
                  api<Bootstrap>('/bootstrap').then(setBoot).catch(handleError);
              }}
            >
              Refresh
            </Button>
          </div>
          {view === 'activity' && (
            <div className="tabs">
              {['calls', 'events'].map((t) => (
                <button
                  key={t}
                  aria-pressed={tab === t}
                  onClick={() => {
                    setTab(t);
                    setSelection(null);
                    setStatus('');
                    setFilters({});
                    setRelatedFilter({});
                    setUser('');
                    setTenant('');
                  }}
                >
                  {t === 'calls' ? 'Calls' : 'Events'}
                </button>
              ))}
              <span>
                Last 24 hours ·{' '}
                {pageCount > 1 ? 'auto-refresh paused on older pages' : 'refreshes every 5s'}
              </span>
            </div>
          )}
          {view === 'settings' && (
            <div className="tabs">
              {['integrations', 'keys', 'server'].map((t) => (
                <button
                  key={t}
                  aria-pressed={settings === t}
                  onClick={() => {
                    setSettings(t);
                    setSelection(null);
                  }}
                >
                  {t === 'keys' ? 'Application keys' : t[0].toUpperCase() + t.slice(1)}
                </button>
              ))}
            </div>
          )}
          {resource && view !== 'settings' && (
            <form
              className="filters"
              onSubmit={(e) => {
                e.preventDefault();
                setSelection(null);
                setFilters({
                  ...(multi && tenant ? { tenant_id: tenant } : {}),
                  ...(user ? { user_id: user } : {}),
                  ...(status && resource !== 'events' ? { status } : {}),
                });
              }}
            >
              {multi && (
                <label>
                  Tenant
                  <input
                    value={tenant}
                    maxLength={128}
                    onChange={(e) => setTenant(e.target.value)}
                    placeholder="All tenants"
                  />
                </label>
              )}
              <label>
                User
                <input
                  value={user}
                  maxLength={128}
                  onChange={(e) => setUser(e.target.value)}
                  placeholder="All users"
                />
              </label>
              {resource !== 'events' && (
                <Choice
                  label="State"
                  value={status}
                  options={[
                    { value: '', label: 'All states' },
                    ...statuses.map((s) => ({ value: s, label: s.replaceAll('_', ' ') })),
                  ]}
                  onChange={setStatus}
                />
              )}
              <button className="apply" type="submit">
                Apply filters
              </button>
              {Object.keys(relatedFilter).length > 0 && (
                <button type="button" className="clear" onClick={() => setRelatedFilter({})}>
                  Clear connection/session filter
                </button>
              )}
            </form>
          )}
          {error && (
            <p className="error banner" role="alert">
              {error} <button onClick={() => setReload((v) => v + 1)}>Retry read</button>
            </p>
          )}
          {resource ? (
            <>
              <div className="table-container" aria-busy={loading}>
                {loading && records.length === 0 ? (
                  <p className="loading" role="status">
                    Loading {resource}…
                  </p>
                ) : records.length > 0 ? (
                  <DataTable
                    columns={columns}
                    rows={records.map((r) => ({ ...r, _key: rowKey(r) }))}
                    rowKey="_key"
                    selectedId={selection ? rowKey(selection.row) : undefined}
                    onRowClick={open}
                  />
                ) : (
                  !error && (
                    <EmptyState
                      title={
                        Object.keys(filters).length
                          ? 'No matching records.'
                          : `No ${resource.replace('-', ' ')} yet.`
                      }
                    >
                      {resource === 'connections' ? (
                        <>
                          Connect GitHub, Slack, or Gmail through your backend or CLI after
                          configuring the integration.{' '}
                          <code className="command">
                            ravn connections connect --integration github --app-key-file … --user …
                          </code>
                          <span className="muted">Personal-token import is also available.</span>
                        </>
                      ) : resource === 'sessions' ? (
                        view === 'permissions' ? (
                          'Permissions are granted per session. Create one, then choose its tools here.'
                        ) : (
                          'Create a session from your trusted backend or CLI after connecting an account.'
                        )
                      ) : resource === 'app-keys' ? (
                        <code className="command">
                          ravn app-key create --app {app} --output /private/path/backend.key
                        </code>
                      ) : (
                        'Only recorded activity appears here. Pre-admission denials are not a complete audit history.'
                      )}
                    </EmptyState>
                  )
                )}
              </div>
              <div className="list-footer">
                <span>
                  {records.length} loaded{asOf ? ' · updated ' + date(asOf) : ''}
                </span>
                {next && (
                  <Button variant="secondary" disabled={loading} onClick={more}>
                    {loading ? 'Loading…' : 'Load more'}
                  </Button>
                )}
              </div>
            </>
          ) : (
            <div className="settings-content">
              {settings === 'integrations' ? (
                boot.integrations
                  .filter((i) => i.app_id === app)
                  .map((i) => (
                    <section key={i.id}>
                      <h2>
                        {i.id} <State value={i.enabled ? 'enabled' : 'disabled'} />
                      </h2>
                      <Facts
                        items={[
                          ['Provider', PROVIDERS[i.connector] || i.connector],
                          ['Transport', 'Remote MCP'],
                          ['Endpoint', i.endpoint],
                          [
                            'OAuth',
                            i.oauth_configured
                              ? i.oauth_profile
                              : 'Not configured · token import only',
                          ],
                          ['Provider callback URL', i.callback_url],
                          [
                            'Schema approval',
                            i.reviewed_tools.length ? 'Pins configured' : 'Not configured',
                          ],
                        ]}
                      />
                      <div className="tool-list">
                        {i.reviewed_tools.map((t) => (
                          <code key={t}>{t}</code>
                        ))}
                      </div>
                      {i.connector === 'slack' && (
                        <p className="muted">
                          Slack MCP requires an internal or Marketplace-published app with MCP
                          enabled. Personal user tokens only; no bot authority.
                        </p>
                      )}
                      {i.connector === 'gmail' && (
                        <p className="muted">
                          Google's Gmail MCP server is in Developer Preview. Gmail read and compose
                          scopes are restricted: external apps need Google verification. Drafts are
                          never sent.
                        </p>
                      )}
                      <p className="muted">
                        Schema pins are operator configuration, not proof of live provider
                        compatibility. Use <code>ravn integration-inspect</code> to review schemas
                        before editing configuration.
                      </p>
                    </section>
                  ))
              ) : (
                <section>
                  <h2>Server</h2>
                  <Facts
                    items={[
                      ['Deployment', boot.deployment_id],
                      ['Version', boot.version],
                      ['Local readiness', boot.ready ? 'Ready' : 'Not ready'],
                      ['Default runtime lifetime', boot.sessions.default_ttl_seconds + ' seconds'],
                      ['Maximum runtime lifetime', boot.sessions.max_ttl_seconds + ' seconds'],
                      ['Console access', 'Loopback · local owner only'],
                      ['Provider compatibility', 'Live verification not performed'],
                      ['OAuth / refresh', 'Implemented · configure provider apps'],
                      ['Write tools', boot.features.writes ? 'Reviewed and pinned' : 'None pinned'],
                      [
                        'Session permissions',
                        boot.features.session_permissions ? 'Editable here' : 'Not available',
                      ],
                    ]}
                  />
                  <p className="muted">
                    Configuration is read-only here. Restart the server to apply file changes.
                  </p>
                </section>
              )}
            </div>
          )}
          {selection && (
            <Detail
              key={selection.kind + rowKey(selection.row)}
              selection={selection}
              onClose={() => setSelection(null)}
              onAct={(item) => {
                setConfirm(item);
                setCompromise(false);
              }}
              onSessions={(id) => linked('connection_id', id, 'sessions')}
              onCalls={(field, id) => linked(field, id, 'activity')}
              onPermission={changePermission}
            />
          )}
          {confirm && (
            <FocusScope
              modal
              label="Confirm access revocation"
              onClose={() => {
                if (!busy) setConfirm(null);
              }}
            >
              <div className={busy ? 'mutation-busy' : ''} aria-busy={busy}>
                <ConfirmModal
                  open
                  title={
                    confirm.kind === 'connections'
                      ? 'Disconnect this account?'
                      : confirm.kind === 'app-keys'
                        ? 'Revoke this application key?'
                        : 'Revoke this runtime session?'
                  }
                  confirmLabel={busy ? 'Committing…' : 'Confirm revocation'}
                  onCancel={() => {
                    if (!busy) setConfirm(null);
                  }}
                  onConfirm={act}
                  consequence="New calls are blocked once revocation commits. Already-admitted calls may still finish."
                >
                  <Facts
                    items={[
                      ['Application', confirm.row.app_id],
                      ['Tenant', confirm.row.tenant_id],
                      ['User', confirm.row.user_id],
                      ['Target', confirm.row.id],
                    ]}
                  />
                  <p>
                    {confirm.kind === 'connections'
                      ? 'All sessions bound to this connection lose access. RAVN deletes its credential copy; it does not revoke the token at GitHub.'
                      : confirm.kind === 'sessions'
                        ? 'Other sessions and the saved connection remain unchanged.'
                        : 'The backend will no longer be able to use this key. Existing runtime sessions survive unless selected below.'}
                  </p>
                  {confirm.kind === 'app-keys' && (
                    <label className="checkbox">
                      <input
                        type="checkbox"
                        checked={compromise}
                        disabled={busy}
                        onChange={(e) => setCompromise(e.target.checked)}
                      />
                      Also revoke every runtime session issued by this key
                    </label>
                  )}
                </ConfirmModal>
              </div>
            </FocusScope>
          )}
          {toast && (
            <Toast open status={toastError ? 'caution' : 'ok'} onDismiss={() => setToast('')}>
              {toast}
            </Toast>
          )}
        </main>
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<ConsoleApp />);
