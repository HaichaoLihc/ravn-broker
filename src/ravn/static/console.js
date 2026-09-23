// Python renders the pages. JavaScript handles small interactions and API forms.
const message = document.querySelector('#message');
function showError(error) {
  message.textContent = error.message || 'The request could not be completed.';
  message.hidden = false;
  message.scrollIntoView({ block: 'center' });
}

async function api(path, body, method = 'POST', revision = '') {
  const response = await fetch(path, {
    method, credentials: 'same-origin', cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || '',
      ...(revision ? { 'If-Match': revision } : {}),
    },
    body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error?.message || 'The request could not be completed.');
  return result;
}

const ticket = new URLSearchParams(location.hash.slice(1)).get('ticket');
if (ticket) {
  history.replaceState(null, '', location.pathname + location.search);
  api('/console/api/v1/auth/exchange', { ticket }).then(() => location.reload()).catch(showError);
}

const integration = document.querySelector('#integration-form');
if (integration) {
  const input = name => integration.elements.namedItem(name);
  const settings = document.querySelector('#integration-settings');
  const oauth = document.querySelector('#oauth-fields');
  const auth = document.querySelector('#integration-auth');
  const options = document.querySelector('#oauth-options');
  const status = document.querySelector('#discovery-status');
  const choices = document.querySelector('#scope-choices');
  const discover = document.querySelector('#discover-integration');
  const manual = document.querySelector('#manual-integration');
  const method = input('oauth.token_endpoint_auth_method');
  let generation = 0;
  function updateAuth() {
    oauth.hidden = oauth.disabled = auth.value !== 'oauth';
    document.querySelector('#bearer-help').hidden = auth.value !== 'bearer';
    const secret = input('oauth.client_secret');
    secret.disabled = method.value === 'none';
    secret.required = !secret.disabled;
    secret.parentElement.hidden = secret.disabled;
    if (secret.disabled) secret.value = '';
  }
  function callback() {
    document.querySelector('#integration-callback').textContent = input('id').value
      ? integration.dataset.callback + encodeURIComponent(input('id').value) : 'Enter an Integration ID above.';
  }
  function reset() {
    generation++;
    settings.hidden = settings.disabled = true;
    status.textContent = '';
    choices.replaceChildren();
    for (const name of ['client_id', 'client_secret', 'authorization_endpoint', 'token_endpoint', 'issuer', 'resource', 'scopes']) input('oauth.' + name).value = '';
    for (const option of method.options) option.disabled = false;
    method.value = 'client_secret_post';
    input('oauth.authorization_params').value = '{}';
    input('oauth.rotating_refresh_tokens').checked = false;
    auth.value = 'oauth';
  }
  input('endpoint').addEventListener('input', reset);
  input('id').addEventListener('input', callback);
  auth.addEventListener('change', updateAuth);
  method.addEventListener('change', updateAuth);
  manual.addEventListener('click', () => {
    generation++;
    if (!input('endpoint').reportValidity() || !input('id').reportValidity()) return;
    settings.hidden = settings.disabled = false;
    for (const option of method.options) option.disabled = false;
    options.open = true;
    status.textContent = 'Manual setup. Choose OAuth or bearer-token import below.';
    updateAuth(); callback();
  });
  discover.addEventListener('click', async () => {
    if (!input('endpoint').reportValidity() || !input('id').reportValidity()) return;
    reset();
    const current = generation;
    discover.disabled = manual.disabled = true;
    status.textContent = 'Reading service settings…';
    try {
      const result = await api('/console/api/v1/integrations/discover', { endpoint: input('endpoint').value });
      if (current !== generation) return;
      for (const name of ['authorization_endpoint', 'token_endpoint', 'issuer', 'resource', 'token_endpoint_auth_method']) input('oauth.' + name).value = result.oauth[name] || '';
      for (const option of method.options) option.disabled = !result.authentication_methods.includes(option.value);
      const scopes = input('oauth.scopes');
      scopes.value = result.oauth.scopes.join(' ');
      for (const scope of [...new Set([...result.supported_scopes, ...result.oauth.scopes])]) {
        const label = document.createElement('label'), checkbox = document.createElement('input');
        label.className = 'check'; checkbox.type = 'checkbox'; checkbox.value = scope;
        checkbox.checked = result.oauth.scopes.includes(scope);
        checkbox.addEventListener('change', () => {
          const selected = new Set(scopes.value.trim().split(/\s+/).filter(Boolean));
          if (checkbox.checked) selected.add(scope); else selected.delete(scope);
          scopes.value = [...selected].join(' ');
        });
        label.append(checkbox, document.createTextNode(scope)); choices.append(label);
      }
      settings.hidden = settings.disabled = false;
      options.open = false;
      status.textContent = 'Authorization settings found. Enter your application credentials and choose permissions.';
      updateAuth(); callback();
    } catch (error) {
      if (current === generation) status.textContent = error.message;
    } finally {
      discover.disabled = manual.disabled = false;
    }
  });
  input('oauth.scopes').addEventListener('input', () => {
    const selected = new Set(input('oauth.scopes').value.trim().split(/\s+/));
    for (const checkbox of choices.querySelectorAll('input')) checkbox.checked = selected.has(checkbox.value);
  });
  integration.addEventListener('invalid', event => {
    if (options.contains(event.target)) options.open = true;
  }, true);
}

const dialog = document.querySelector('#confirm');
async function confirmChange(form) {
  if (!form.dataset.confirm) return true;
  const question = form.dataset.confirm.indexOf('?');
  dialog.querySelector('#confirm-title').textContent = form.dataset.confirm.slice(0, question + 1) || 'Confirm access change';
  dialog.querySelector('#confirm-description').textContent = form.dataset.confirm.slice(question + 1).trim();
  const facts = dialog.querySelector('#confirm-facts');
  facts.replaceChildren();
  for (const [label, value] of [['Application', form.elements.app_id?.value], ['Tenant', form.elements.tenant_id?.value], ['User', form.dataset.user], ['Target', form.dataset.target]]) {
    if (!value) continue;
    const term = document.createElement('dt'), description = document.createElement('dd');
    term.textContent = label;
    description.textContent = value;
    facts.append(term, description);
  }
  const option = dialog.querySelector('#confirm-key');
  option.hidden = !form.elements.revoke_sessions;
  option.querySelector('input').checked = false;
  dialog.returnValue = '';
  const answer = new Promise(resolve => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'confirm'), { once: true }));
  dialog.showModal();
  const accepted = await answer;
  if (form.elements.revoke_sessions) form.elements.revoke_sessions.value = String(option.querySelector('input').checked);
  return accepted;
}

for (const form of document.querySelectorAll('form[data-method]')) {
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (form === integration && document.querySelector('#integration-settings').disabled) return;
    if (form.dataset.busy || dialog?.open) return;
    if (!await confirmChange(form)) return;
    const buttons = [...form.querySelectorAll('button')];
    let path = form.getAttribute('action');
    try {
      const body = {};
      for (const input of form.elements) {
        if (!input.name || input.matches(':disabled')) continue;
        if (input.name === '_connection') {
          path = path.replace('{connection}', encodeURIComponent(input.value));
          continue;
        }
        let value = input.type === 'checkbox' ? input.checked : input.value;
        if (input.dataset.format === 'json') value = JSON.parse(value || '{}');
        if (input.dataset.format === 'words') value = value.trim().split(/\s+/).filter(Boolean);
        const [group, key] = input.name.split('.');
        if (key) (body[group] ??= {})[key] = value;
        else body[group] = value;
      }
      form.dataset.busy = 'true';
      buttons.forEach(button => { button.disabled = true; });
      message.hidden = true;
      const result = await api(path, body, form.dataset.method, form.dataset.revision);
      if (form.hasAttribute('data-key')) {
        document.querySelector('#generated-key').value = result.key;
        document.querySelector('#new-key').showModal();
        return;
      }
      try { sessionStorage.setItem('ravn-toast', 'Change saved.'); } catch {}
      if (form.dataset.nextView) location.assign('/console/?view=' + encodeURIComponent(form.dataset.nextView) + '&app_id=' + encodeURIComponent(result.id));
      else location.reload();
    } catch (error) {
      showError(error instanceof SyntaxError ? new Error('JSON fields must contain valid JSON objects.') : error);
    } finally {
      form.querySelectorAll('input[type="password"]').forEach(input => { input.value = ''; });
      delete form.dataset.busy;
      buttons.forEach(button => { button.disabled = false; });
    }
  });
}

const newKey = document.querySelector('#new-key');
newKey?.addEventListener('close', () => {
  document.querySelector('#generated-key').value = '';
  location.reload();
});
document.querySelector('#copy-new-key')?.addEventListener('click', async event => {
  try {
    await navigator.clipboard.writeText(document.querySelector('#generated-key').value);
    event.target.textContent = 'Copied';
  } catch { event.target.textContent = 'Select and copy the key'; }
});
window.addEventListener('pagehide', () => { if (newKey) document.querySelector('#generated-key').value = ''; });

for (const time of document.querySelectorAll('time[datetime]')) {
  const date = new Date(time.dateTime);
  if (!Number.isNaN(date.getTime())) { time.title = time.dateTime; time.textContent = date.toLocaleString(); }
}
for (const select of document.querySelectorAll('[data-submit]')) select.addEventListener('change', () => select.form.requestSubmit());

try {
  const hidden = localStorage.getItem('ravn-sidebar-hidden');
  document.body.classList.toggle('sidebar-hidden', hidden === 'true' || (hidden === null && matchMedia('(max-width:580px)').matches));
} catch {}
function toggleSidebar() {
  const hidden = document.body.classList.toggle('sidebar-hidden');
  try { localStorage.setItem('ravn-sidebar-hidden', String(hidden)); } catch {}
}
for (const button of document.querySelectorAll('[data-sidebar]')) button.addEventListener('click', toggleSidebar);
const drawer = document.querySelector('.drawer');
drawer?.focus({ preventScroll: true });
document.addEventListener('keydown', event => {
  if ((event.metaKey || event.ctrlKey) && event.key === '\\') { event.preventDefault(); toggleSidebar(); }
  if (event.key === 'Escape' && drawer && !dialog?.open) location.assign(drawer.dataset.closeUrl);
});
for (const row of document.querySelectorAll('tbody tr')) row.addEventListener('click', event => {
  if (!event.target.closest('a,button,input,select')) row.querySelector('.row-link')?.click();
});

const toast = document.querySelector('#toast');
function notify(text) {
  if (!toast) return;
  toast.querySelector('span:not(.icon)').textContent = text;
  toast.hidden = false;
  setTimeout(() => { toast.hidden = true; }, 4000);
}
toast?.querySelector('button').addEventListener('click', () => { toast.hidden = true; });
try { const text = sessionStorage.getItem('ravn-toast'); sessionStorage.removeItem('ravn-toast'); if (text) notify(text); } catch {}
for (const button of document.querySelectorAll('[data-copy]')) button.addEventListener('click', async () => {
  try { await navigator.clipboard.writeText(button.dataset.copy); notify('Copied.'); } catch { showError(new Error('Could not copy. Select the ID to copy it manually.')); }
});

let edited = false;
document.addEventListener('input', () => { edited = true; });
document.addEventListener('change', () => { edited = true; });
if (document.body.hasAttribute('data-auto-refresh')) setInterval(() => {
  if (!document.hidden && !edited && !dialog?.open && !document.querySelector('[data-busy]') && !document.activeElement.matches('input,select,button')) location.reload();
}, 5000);
