'use strict';
const $ = id => document.getElementById(id);
const BASE = document.documentElement.dataset.basePath || '';
const API = `${BASE}/api`;
const FIELDS = ['workstream', 'intervention', 'owner', 'edge_owner', 'status', 'target_date', 'progress', 'blocker'];
const STATUS = {
  'Pending Confirmation': {color: '--pending'},
  'Not Started': {color: '--not-started'},
  'In Progress': {color: '--progress'},
  'Blocked': {color: '--blocked'},
  'Delivered': {color: '--delivered'},
  'On Hold / Cancelled': {color: '--hold'}
};
const CHART_ORDER = ['In Progress', 'Not Started', 'Pending Confirmation', 'Blocked', 'Delivered', 'On Hold / Cancelled'];
const ICON_PATHS = {
  plus: '<path d="M12 5v14M5 12h14"/>',
  save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h12l4 4v12a2 2 0 0 1-2 2Z"/><path d="M17 21v-8H7v8M7 3v5h9V3"/>',
  moon: '<path d="M20.9 13a9 9 0 0 1-9.9-9.9A9 9 0 1 0 20.9 13Z"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Zm0 0v6h6M8 13h8M8 17h6"/>',
  layers: '<path d="m12 3 10 6-10 6L2 9Zm-10 11 10 6 10-6M2 19l10 6 10-6" transform="translate(0 -1) scale(1 .9)"/>',
  trend: '<path d="m3 17 6-6 4 4 8-10M15 5h6v6"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  flag: '<path d="M4 22V3m0 1c4-3 8 3 12 0l4-1v11l-4 1c-4 3-8-3-12 0"/>',
  filter: '<path d="M4 5h16M7 12h10m-7 7h4"/>',
  chart: '<path d="M4 3v17h17M9 15V9m5 6V5m5 10v-5"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10v1"/>',
  alert: '<path d="m12 3 10 18H2Zm0 5v6m0 3v1"/>',
  spark: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>',
  edit: '<path d="m15 4 5 5M4 20l5-1L21 7a2.8 2.8 0 0 0-4-4L5 15ZM4 20h9"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  sort: '<path d="M8 4v16m-4-4 4 4 4-4M15 5h6m-6 5h4m-4 5h2"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>'
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON_PATHS[name] || ''}</svg>`;
const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
const option = (value, text = value) => `<option value="${esc(value)}">${esc(text)}</option>`;
const colorStyle = status => `--status-color:var(${STATUS[status]?.color || '--not-started'})`;
const badge = status => `<span class="status-badge" style="${colorStyle(status)}"><i></i>${esc(status)}</span>`;
// Normalize spelling variants so "and"/"&" and typos never create duplicate groups.
const canonical = name => String(name ?? '')
  .replace('Tatical', 'Tactical')
  .replace(/\band\b/g, '&')
  .replace('post-buy', 'postbuy')
  .replace(/\s+/g, ' ')
  .trim();
// Fixed display order defined by the workstream reference list. Unknown streams
// sort after the known ones, alphabetically, so new workstreams never break the layout.
const WORKSTREAM_ORDER = ['mediastrategycampaignplanning', 'tacticalplan', 'performancetrackingpostbuy',
  'grbillingcollection', 'prepayment', 'enabler'];
const streamKey = name => canonical(name).toLowerCase().replace(/[^a-z0-9]/g, '');
const streamRank = name => {
  const index = WORKSTREAM_ORDER.indexOf(streamKey(name));
  return index === -1 ? WORKSTREAM_ORDER.length : index;
};
const orderedStreams = rows => [...new Set(rows.map(row => canonical(row.workstream)))]
  .sort((a, b) => (streamRank(a) - streamRank(b)) || a.localeCompare(b));
const two = number => String(number).padStart(2, '0');
const active = row => !['Delivered', 'On Hold / Cancelled'].includes(row.status);
const overdue = row => !!row.target_date && row.target_date < DATA.today && active(row);
const attention = row => row.status === 'Blocked' || overdue(row);
const issueMatch = (row, issue) => ({undated: active(row) && !row.target_date, unowned: active(row) && !row.owner, support: !!row.blocker, attention: attention(row)}[issue] ?? true);
const ISSUE_NAMES = {undated: 'Missing dates', unowned: 'Missing owners', support: 'Support needed', attention: 'Needs attention'};
let DATA = null, records = [], drafts = new Map(), baseRevision = null;
let view = 'overview', issue = '', editingId = null, editorOriginal = '', saving = false, dateSort = 0, toastTimer;
let draftKey = '', draftStorageFailed = false;

function storageGet(key) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function storageSet(key, value) {
  try { localStorage.setItem(key, value); return true; } catch { return false; }
}
function removeDraftStorage() {
  try { localStorage.removeItem(draftKey); } catch { /* In-memory drafts remain usable. */ }
}
function toast(text) {
  clearTimeout(toastTimer);
  $('toast').textContent = text;
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 3800);
}
function showError(text, conflict = false) {
  $('errorText').textContent = text;
  $('errorBanner').hidden = false;
  $('exportDraft').hidden = drafts.size === 0;
  if (conflict) $('retryButton').textContent = 'Reload';
}
function fieldsOf(row) { return Object.fromEntries(FIELDS.map(key => [key, row[key] || ''])); }
function mergeRecords() {
  records = DATA.records.map(row => drafts.has(row.id) ? {...row, ...drafts.get(row.id)} : row);
  for (const [id, row] of drafts) if (id.startsWith('new-')) records.push({...row, id, source_row: null});
}
function persistDrafts() {
  if (!drafts.size) return removeDraftStorage();
  if (!storageSet(draftKey, JSON.stringify({revision: baseRevision, changes: [...drafts.values()], savedAt: new Date().toISOString()})) && !draftStorageFailed) {
    draftStorageFailed = true;
    showError('Draft backup unavailable. Keep this page open and save or export your changes.');
  }
}
function renderDraftState() {
  const n = drafts.size;
  document.body.classList.toggle('has-drafts', n > 0);
  $('draftDock').hidden = n === 0;
  $('discardButton').hidden = n === 0;
  $('draftCount').textContent = `${n} unsaved ${n === 1 ? 'change' : 'changes'}`;
  $('saveButton').disabled = !n || saving;
  $('saveButton').classList.toggle('has-changes', n > 0);
  $('saveLabel').textContent = saving ? 'Saving…' : n ? `Save changes (${n})` : 'Save changes';
  $('dockSave').disabled = saving;
  $('exportDraft').hidden = !n;
  $('addButton').disabled = !DATA || saving;
}
function renderMeta() {
  $('lastSaved').textContent = new Date(DATA.meta.updated_at).toLocaleString('en-GB', {timeZone: 'Asia/Shanghai', hour12: false, year: 'numeric', month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit'});
  $('asOfDate').textContent = DATA.today.replaceAll('-', ' / ');
  $('connection').className = 'connection live';
  $('connection').innerHTML = '<i></i><span>Connected</span>';
}
function renderMetrics() {
  const total = records.length, delivered = records.filter(r => r.status === 'Delivered').length;
  const streams = new Set(records.map(r => canonical(r.workstream)));
  const rate = total ? Math.round(delivered / total * 100) : 0;
  $('metricTotal').textContent = two(total);
  $('metricStreams').textContent = `${streams.size} ${streams.size === 1 ? 'workstream' : 'workstreams'}`;
  $('metricProgress').textContent = two(records.filter(r => r.status === 'In Progress').length);
  $('metricDelivered').textContent = two(delivered);
  $('metricDeliveryRate').textContent = `${rate}% delivery rate`;
  $('metricAttention').textContent = two(records.filter(attention).length);
  $('deliveryPercent').innerHTML = `${rate}<small>%</small>`;
  $('tabCount').textContent = total;
}
function renderCharts() {
  $('workstreamLegend').innerHTML = CHART_ORDER.map(status => `<span class="legend-item" style="${colorStyle(status)}"><i class="legend-swatch"></i>${esc(status)}</span>`).join('');
  const streams = orderedStreams(records);
  $('workstreamChart').innerHTML = streams.map(stream => {
    const rows = records.filter(row => canonical(row.workstream) === stream);
    const counts = CHART_ORDER.map(status => [status, rows.filter(row => row.status === status).length]);
    const description = `${stream}: ${rows.length} interventions, ${counts.map(([status, n]) => `${status} ${n}`).join(', ')}`;
    return `<button class="workstream-row" data-stream="${esc(stream)}" aria-label="${esc(description)}">
      <span class="workstream-label"><span class="workstream-name">${esc(stream)}</span><span class="workstream-total">${icon('arrow')}</span></span>
      <span class="stacked-bar" role="img" aria-label="${esc(description)}">${counts.filter(([, n]) => n).map(([status, n]) => `<span class="bar-segment" title="${esc(status)}: ${n}" style="${colorStyle(status)};width:${n / rows.length * 100}%"></span>`).join('')}</span>
      <span class="workstream-meta"><span>${rows.filter(row => row.status === 'Delivered').length}/${rows.length} delivered</span><span>${rows.filter(row => !row.target_date && active(row)).length} undated</span></span>
    </button>`;
  }).join('') || '<div class="empty-state">No workstreams yet. Add an intervention to get started.</div>';
  $('statusChart').innerHTML = CHART_ORDER.map(status => {
    const n = records.filter(row => row.status === status).length;
    return `<button class="status-row" data-status="${esc(status)}" style="${colorStyle(status)}" aria-label="${esc(status)}: ${n} interventions. View details"><span class="status-label"><i class="legend-swatch"></i>${esc(status)}</span><span class="status-bar-track"><span class="status-bar-fill" style="width:${records.length ? n / records.length * 100 : 0}%"></span></span><span class="status-count">${two(n)}</span></button>`;
  }).join('');
}
function renderOptions() {
  const selectedStream = $('workstreamFilter').value, selectedStatus = $('statusFilter').value;
  const groups = orderedStreams(records);
  $('workstreamFilter').innerHTML = option('', 'All workstreams') + groups.map(value => option(value)).join('');
  $('statusFilter').innerHTML = option('', 'All statuses') + DATA.statuses.map(value => option(value)).join('');
  $('workstreamFilter').value = groups.includes(selectedStream) ? selectedStream : '';
  $('statusFilter').value = selectedStatus;
  // One canonical, de-duplicated, ordered source for the editor suggestion list.
  // Includes workstreams from the Options sheet (e.g. Pre-payment) even when no
  // record uses them yet, and merges spelling variants from existing records.
  const optionRows = [...(DATA.meta.workstream_options || []), ...records.map(row => row.workstream)]
    .map(workstream => ({workstream}));
  const editorStreams = orderedStreams(optionRows);
  $('workstreamOptions').innerHTML = editorStreams.map(value => option(value)).join('');
  $('editorForm').elements.status.innerHTML = DATA.statuses.map(value => option(value)).join('');
}
function renderTable() {
  const query = $('searchInput').value.trim().toLocaleLowerCase();
  const stream = $('workstreamFilter').value, status = $('statusFilter').value;
  let filtered = records.filter(row => (!stream || canonical(row.workstream) === stream) && (!status || row.status === status) && (!issue || issueMatch(row, issue)) && (!query || [...FIELDS.map(key => row[key]), canonical(row.workstream)].join(' ').toLocaleLowerCase().includes(query)));
  if (dateSort) filtered = [...filtered].sort((a, b) => {
    if (!a.target_date || !b.target_date) return (!a.target_date) - (!b.target_date);
    return a.target_date.localeCompare(b.target_date) * dateSort;
  });
  const preview = view === 'overview' && !query && !stream && !status && !issue;
  const shown = preview ? filtered.slice(0, 5) : filtered;
  $('resultCount').textContent = `${filtered.length} ${filtered.length === 1 ? 'intervention' : 'interventions'}${drafts.size ? ' · Unsaved changes' : ''}`;
  $('tableSummary').textContent = `${shown.length ? `1–${shown.length}` : '0'} of ${filtered.length}`;
  $('viewAllButton').hidden = view === 'tracker';
  $('clearFilters').hidden = !query && !stream && !status && !issue && !dateSort;
  $('issueFilter').hidden = !issue;
  $('issueFilter').innerHTML = issue ? `${icon('filter')}<span>${esc(ISSUE_NAMES[issue])}</span><button id="clearIssue" aria-label="Clear follow-up filter">${icon('close')}</button>` : '';
  $('sortDate').setAttribute('aria-label', dateSort === 1 ? 'Sort target date descending' : 'Sort target date ascending');
  $('sortDate').closest('th').setAttribute('aria-sort', dateSort === 1 ? 'ascending' : dateSort === -1 ? 'descending' : 'none');
  $('recordRows').innerHTML = shown.map(row => {
    const number = records.indexOf(row) + 1, dirty = drafts.has(row.id), isOverdue = overdue(row);
    const initials = row.owner ? row.owner.split(/\s+/).map(part => part[0]).slice(0, 2).join('').toUpperCase() : '';
    return `<tr class="${dirty ? 'dirty-row' : ''}" data-row-id="${esc(row.id)}">
      <td class="number-cell">${two(number)}${dirty ? '<span class="dirty-label">Draft</span>' : ''}</td>
      <td><button class="intervention-link" data-edit="${esc(row.id)}">${esc(row.intervention)}</button><div class="workstream-tag">${esc(canonical(row.workstream))}</div></td>
      <td><div class="owner-name">${row.owner ? `<span class="owner-avatar" aria-hidden="true">${esc(initials)}</span>${esc(row.owner)}` : '<span class="date-sub">Unassigned</span>'}</div><div class="edge-owner">EDGE · ${esc(row.edge_owner || 'Unassigned')}</div></td>
      <td>${badge(row.status)}${isOverdue ? '<span class="date-sub" style="color:var(--danger)">Overdue</span>' : ''}</td>
      <td><span class="date-value ${isOverdue ? 'overdue' : ''}">${esc(row.target_date || '—')}</span>${!row.target_date ? '<span class="date-sub">No date set</span>' : ''}</td>
      <td><div class="progress-text">${esc(row.progress || 'No update yet')}</div>${row.blocker ? `<span class="support-indicator" title="${esc(row.blocker)}">${icon('flag')}Support needed</span>` : ''}</td>
      <td class="edit-cell"><button class="edit-record" data-edit="${esc(row.id)}" aria-label="Edit ${esc(row.intervention)}" title="Edit intervention">${icon('edit')}</button></td>
    </tr>`;
  }).join('') || `<tr><td colspan="7" class="empty-state">${records.length ? 'No matching interventions. Try clearing the filters.' : 'No interventions yet. Add one to get started.'}</td></tr>`;
}
function renderAll() {
  mergeRecords();
  renderMeta();
  renderMetrics();
  renderCharts();
  renderOptions();
  renderDraftState();
  renderTable();
}
function setView(nextView, scroll = false) {
  view = nextView;
  for (const [name, button, panel] of [['overview', 'tabOverview', 'overviewPanel'], ['tracker', 'tabTracker', 'trackerPanel']]) {
    const selected = view === name;
    $(button).classList.toggle('active', selected);
    $(button).setAttribute('aria-selected', selected);
    $(button).tabIndex = selected ? 0 : -1;
    $(panel).hidden = !selected;
  }
  renderTable();
  if (scroll) document.querySelector('.section-navigation').scrollIntoView();
}
function jumpTo({stream = '', status = '', issueKey = ''} = {}) {
  $('searchInput').value = '';
  $('workstreamFilter').value = stream;
  $('statusFilter').value = status;
  issue = issueKey;
  setView('tracker');
  document.querySelector('.section-navigation').scrollIntoView({behavior: 'smooth', block: 'start'});
}
function clearFilters() {
  $('searchInput').value = '';
  $('workstreamFilter').value = '';
  $('statusFilter').value = '';
  issue = '';
  dateSort = 0;
  renderTable();
}
function formValues() {
  const data = new FormData($('editorForm'));
  return Object.fromEntries(FIELDS.map(key => [key, String(data.get(key) || '').trim()]));
}
function openEditor(id = null) {
  if (!DATA || saving) return;
  editingId = id;
  const row = records.find(record => record.id === id);
  const form = $('editorForm');
  form.reset();
  for (const key of FIELDS) form.elements[key].value = row?.[key] || (key === 'status' ? 'Pending Confirmation' : '');
  $('editorTitle').textContent = row ? 'Edit intervention' : 'Add intervention';
  $('editorError').hidden = true;
  editorOriginal = JSON.stringify(formValues());
  $('editor').showModal();
  setTimeout(() => form.elements.intervention.focus(), 0);
}
function editorChanged() { return $('editor').open && JSON.stringify(formValues()) !== editorOriginal; }
function closeEditor() {
  if (editorChanged() && !confirm('Discard changes in this editor?')) return;
  $('editor').close();
}
function applyEditor(event) {
  event.preventDefault();
  const row = formValues();
  if (!row.workstream || !row.intervention) {
    $('editorError').textContent = 'Enter an intervention and workstream.';
    $('editorError').hidden = false;
    return;
  }
  const id = editingId || `new-${crypto.randomUUID()}`;
  const original = DATA.records.find(record => record.id === id);
  if (original && JSON.stringify(fieldsOf(original)) === JSON.stringify(row)) drafts.delete(id);
  else drafts.set(id, {id, ...row});
  persistDrafts();
  $('editor').close();
  renderAll();
  if (!editingId) { clearFilters(); setView('tracker'); }
  toast(drafts.has(id) ? 'Draft updated. Save changes to finish.' : 'No changes to save.');
}
async function save() {
  if (!drafts.size || saving) return;
  saving = true;
  renderDraftState();
  try {
    const response = await fetch(`${API}/save`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({revision: baseRevision, changes: [...drafts.values()]})});
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.error || 'Save failed. Please retry.');
      error.conflict = response.status === 409;
      throw error;
    }
    DATA = result;
    baseRevision = DATA.meta.revision;
    drafts.clear();
    removeDraftStorage();
    $('errorBanner').hidden = true;
    $('restoredBanner').hidden = true;
    renderAll();
    toast(`Saved ${result.saved_count} ${result.saved_count === 1 ? 'change' : 'changes'}.`);
  } catch (error) {
    persistDrafts();
    showError(error.message || 'Cannot connect. Your draft is safe.', error.conflict);
    $('connection').className = 'connection offline';
    $('connection').innerHTML = '<i></i><span>Not saved · Draft retained</span>';
    toast('Save failed. Your draft is safe.');
  } finally {
    saving = false;
    renderDraftState();
  }
}
function exportDraft() {
  const blob = new Blob([JSON.stringify({source: DATA.meta.source_name, source_hash: DATA.meta.source_hash, revision: baseRevision, changes: [...drafts.values()]}, null, 2)], {type: 'application/json'});
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  const brandSlug = document.documentElement.dataset.brandSlug || 'PM-McKinsey';
  link.download = `${brandSlug}-draft-${DATA.today}.json`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function loadData(reload = false) {
  try {
    const response = await fetch(`${API}/data`, {cache: 'no-store'});
    if (!response.ok) throw new Error('Unable to load interventions.');
    const result = await response.json();
    if (reload && drafts.size && !confirm('Reload and discard unsaved changes? Export your draft first if needed.')) return;
    DATA = result;
    $('errorBanner').hidden = true;
    baseRevision = DATA.meta.revision;
    draftKey = `pm-mckinsey:${location.origin}:${DATA.meta.source_hash}`;
    if (reload) {
      drafts.clear();
      removeDraftStorage();
      $('restoredBanner').hidden = true;
    } else {
      const stored = storageGet(draftKey);
      if (stored) {
        try {
          const parsed = JSON.parse(stored);
          if (!Array.isArray(parsed.changes) || !Number.isInteger(parsed.revision)) throw new Error('Invalid draft');
          const valid = parsed.changes.every(row => row && typeof row.id === 'string' && FIELDS.every(key => typeof row[key] === 'string') && (row.id.startsWith('new-') || DATA.records.some(record => record.id === row.id)) && STATUS[row.status]);
          if (!valid) throw new Error('Invalid draft');
          drafts = new Map(parsed.changes.map(row => [row.id, {id: row.id, ...fieldsOf(row)}]));
          baseRevision = parsed.revision;
          $('restoredText').textContent = `Restored ${drafts.size} unsaved ${drafts.size === 1 ? 'change' : 'changes'}.`;
          $('restoredBanner').hidden = !drafts.size;
        } catch {
          showError('Draft backup could not be read. It has not been overwritten.');
        }
      }
    }
    if (drafts.size && baseRevision !== DATA.meta.revision) showError('Newer data is available. Export your draft before reloading.', true);
    for (const element of document.querySelectorAll('.metric, .tab, [data-issue], #searchInput, #workstreamFilter, #statusFilter, #viewAllButton')) element.disabled = false;
    renderAll();
  } catch (error) {
    showError(`${error.message} Check the local server and reconnect.`);
    $('connection').className = 'connection offline';
    $('connection').innerHTML = '<i></i><span>Disconnected</span>';
    if (!DATA) {
      $('recordRows').innerHTML = '<tr><td colspan="7" class="empty-state">Cannot connect. Start the local server and retry.</td></tr>';
      $('resultCount').textContent = 'Disconnected';
      $('workstreamChart').innerHTML = '<div class="empty-state">Waiting for data</div>';
    }
  }
}
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  storageSet('pm-mckinsey:theme', theme);
  const label = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode';
  $('themeToggle').setAttribute('aria-label', label);
  $('themeToggle').title = label;
  $('themeToggle').innerHTML = icon(theme === 'dark' ? 'sun' : 'moon');
}

for (const element of document.querySelectorAll('[data-icon]')) element.innerHTML = icon(element.dataset.icon);
setTheme(storageGet('pm-mckinsey:theme') === 'dark' ? 'dark' : 'light');
$('themeToggle').addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));
$('addButton').addEventListener('click', () => openEditor());
$('saveButton').addEventListener('click', save);
$('dockSave').addEventListener('click', save);
$('editorForm').addEventListener('submit', applyEditor);
for (const id of ['editorClose', 'editorCancel']) $(id).addEventListener('click', closeEditor);
$('editor').addEventListener('cancel', event => { event.preventDefault(); closeEditor(); });
$('tabOverview').addEventListener('click', () => setView('overview'));
$('tabTracker').addEventListener('click', () => setView('tracker'));
$('viewAllButton').addEventListener('click', () => jumpTo());
for (const id of ['searchInput', 'workstreamFilter', 'statusFilter']) $(id).addEventListener(id === 'searchInput' ? 'input' : 'change', () => DATA && renderTable());
$('clearFilters').addEventListener('click', clearFilters);
$('sortDate').addEventListener('click', () => { if (DATA) { dateSort = dateSort === 1 ? -1 : 1; renderTable(); } });
$('retryButton').addEventListener('click', () => loadData(true));
$('exportDraft').addEventListener('click', exportDraft);
$('dismissRestored').addEventListener('click', () => { $('restoredBanner').hidden = true; });
$('discardButton').addEventListener('click', () => {
  if (saving || !confirm('Discard all drafts? Saved data will not change.')) return;
  drafts.clear();
  baseRevision = DATA.meta.revision;
  removeDraftStorage();
  $('restoredBanner').hidden = true;
  $('errorBanner').hidden = true;
  renderAll();
});
document.addEventListener('click', event => {
  const target = event.target.closest('button');
  if (!target || !DATA) return;
  if (target.dataset.edit) openEditor(target.dataset.edit);
  if (target.dataset.stream) jumpTo({stream: target.dataset.stream});
  if (target.dataset.status) jumpTo({status: target.dataset.status});
  if (target.dataset.issue) jumpTo({issueKey: target.dataset.issue});
  if (target.dataset.metric) {
    const value = target.dataset.metric;
    jumpTo(value === 'all' ? {} : value === 'attention' ? {issueKey: 'attention'} : {status: value});
  }
  if (target.id === 'clearIssue') { issue = ''; renderTable(); }
});
document.querySelector('.tabs').addEventListener('keydown', event => {
  if (!DATA || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
  event.preventDefault();
  const next = event.key === 'Home' ? 'overview' : event.key === 'End' ? 'tracker' : view === 'overview' ? 'tracker' : 'overview';
  setView(next);
  $(next === 'overview' ? 'tabOverview' : 'tabTracker').focus();
});
document.addEventListener('keydown', event => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && DATA && !$('editor').open) {
    event.preventDefault();
    if (view !== 'tracker') setView('tracker');
    $('searchInput').focus();
    $('searchInput').scrollIntoView({block: 'center'});
  }
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's' && DATA) {
    event.preventDefault();
    if ($('editor').open) toast('Apply your draft before saving.');
    else save();
  }
});
window.addEventListener('beforeunload', event => {
  if (drafts.size || editorChanged()) { event.preventDefault(); event.returnValue = ''; }
});
loadData();
