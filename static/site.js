document.addEventListener('DOMContentLoaded', () => {
  const navToggle = document.getElementById('mobileNavToggle');
  const nav = document.getElementById('primaryNav');
  navToggle?.addEventListener('click', () => {
    const open = navToggle.getAttribute('aria-expanded') === 'true';
    navToggle.setAttribute('aria-expanded', String(!open));
    const sidebar = document.getElementById('workspaceSidebar');
    if (sidebar) sidebar.classList.toggle('mobile-open', !open);
    else nav.classList.toggle('nav-open', !open);
  });

  document.querySelectorAll('[data-logout]').forEach((button) => {
    button.addEventListener('click', async () => {
      const dialog = document.getElementById('logoutDialog');
      if (!dialog) return;
      const confirmed = await new Promise((resolve) => {
        dialog.addEventListener('close', () => resolve(dialog.returnValue === 'logout'), { once: true });
        dialog.showModal();
      });
      if (!confirmed) return;
      button.disabled = true;
      try {
        await fetch('/api/auth/logout', { method: 'POST' });
      } finally {
        window.location.assign('/');
      }
    });
  });

  const requestJson = async (url, options = {}) => {
    const response = await fetch(url, options);
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || 'Request failed.');
    return body;
  };
  const setMessage = (element, text, kind) => {
    if (!element) return;
    element.textContent = text;
    element.className = `form-message ${kind || ''}`;
  };
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const link = (href, text, className = '') => {
    const element = node('a', text, className);
    element.href = href;
    return element;
  };
  const formatDate = (value) => value && value !== 'Not detected' ? value : 'Date not detected';

  async function fillDashboard() {
    const recent = document.getElementById('dashboardRecent');
    if (!recent) return;
    try {
      const data = await requestJson('/api/dashboard');
      const records = data.recent || [];
      document.getElementById('statDocuments').textContent = data.document_count;
      document.getElementById('statTimeline').textContent = records.length ? formatDate(records[0].document_date) : '—';
      document.getElementById('statCategories').textContent = (data.categories || []).length;
      const recentLabel = document.getElementById('recentCountLabel');
      if (recentLabel) recentLabel.textContent = `Showing ${data.recent_count} recent of ${data.document_count} saved ${data.document_count === 1 ? 'document' : 'documents'}.`;
      const categories = document.getElementById('dashboardCategories');
      categories.replaceChildren();
      if (data.categories?.length) {
        data.categories.forEach((category) => {
          const row = link(`/timeline?category=${encodeURIComponent(category.document_type || 'Uncategorized')}`, '', 'category-row');
          row.append(node('span', '▤', 'category-symbol'), node('span', category.document_type || 'Uncategorized', 'category-name'), node('b', String(category.count), 'category-count'));
          categories.append(row);
        });
      } else {
        categories.append(node('p', 'Categories will appear when you save documents.', 'empty-copy'));
      }
      recent.replaceChildren();
      if (!records.length) {
        const empty = node('div', undefined, 'empty-state');
        empty.append(node('span', '▤', 'empty-state-icon'), node('h3', 'Your workspace is ready'), node('p', 'Upload a document and review the extracted fields, or load fictional demo records to explore the timeline.'));
        empty.append(link('/documents', 'Upload your first document →', 'text-link'));
        recent.append(empty);
      } else {
        records.forEach((record) => {
          const item = link(`/records/${record.id}`, '', 'recent-item');
          item.append(node('span', '▤', 'recent-icon'), node('span', undefined, 'recent-copy'));
          const copy = item.querySelector('.recent-copy');
          copy.append(node('b', record.document_type || 'Medical document'), node('small', `${formatDate(record.document_date)} · ${record.filename}`));
          if (record.is_synthetic) item.append(node('span', 'Synthetic', 'tag synthetic'));
          else item.append(node('span', record.status || 'Saved', 'tag original'));
          recent.append(item);
        });
      }
      const latest = document.getElementById('latestDate');
      if (latest) latest.textContent = records[0] ? formatDate(records[0].document_date) : '—';
    } catch (error) {
      recent.replaceChildren(node('p', error.message, 'error-copy'));
    }
  }

  async function fillTimeline() {
    const container = document.getElementById('timeline');
    if (!container) return;
    try {
      const selectedCategory = container.dataset.category || new URLSearchParams(window.location.search).get('category') || '';
      const data = await requestJson(`/api/timeline${selectedCategory ? `?category=${encodeURIComponent(selectedCategory)}` : ''}`);
      const records = data.records || [];
      const count = document.getElementById('timelineCount');
      if (count) count.textContent = `${records.length} ${records.length === 1 ? 'record' : 'records'}`;
      const note = document.getElementById('timelineFilterNote');
      if (note && selectedCategory) note.textContent = `Filtered by ${selectedCategory}`;
      container.replaceChildren();
      if (!records.length) {
        const empty = node('div', undefined, 'empty-state');
        empty.append(node('span', '◷', 'empty-state-icon'), node('h3', selectedCategory ? 'No documents in this category' : 'No saved records yet'), node('p', selectedCategory ? 'Try another category or clear the filter to see all of your records.' : 'After reviewing an upload, save it here to build your health timeline.'));
        if (selectedCategory) empty.append(link('/timeline', 'Clear category filter →', 'text-link'));
        else empty.append(link('/documents', 'Upload a document →', 'text-link'));
        container.append(empty);
        return;
      }
      records.forEach((record) => {
        const item = link(`/records/${record.id}`, '', 'timeline-item');
        const marker = node('span', '', 'timeline-marker');
        const content = node('span', undefined, 'timeline-content');
        const header = node('span', undefined, 'timeline-item-head');
        header.append(node('b', record.document_type || 'Medical document'), node('span', record.is_synthetic ? 'Synthetic sample' : (record.status || 'Saved'), `tag ${record.is_synthetic ? 'synthetic' : 'original'}`));
        content.append(node('small', formatDate(record.document_date), 'timeline-date'), header, node('span', record.filename, 'timeline-filename'), node('span', 'Open record details →', 'timeline-open'));
        item.append(marker, content);
        container.append(item);
      });
    } catch (error) {
      container.replaceChildren(node('p', error.message, 'error-copy'));
    }
  }

  async function fillRecord() {
    const container = document.getElementById('recordDetails');
    if (!container) return;
    const recordId = document.querySelector('[data-record-id]')?.dataset.recordId;
    try {
      const record = await requestJson(`/api/records/${recordId}`);
      const loading = document.getElementById('recordLoading');
      if (loading) loading.classList.add('hidden');
      container.classList.remove('hidden');
      document.getElementById('recordSummary').textContent = record.summary || 'Not detected';
      const frameUrl = `/api/records/${recordId}/file`;
      document.getElementById('originalDocument').src = frameUrl;
      document.getElementById('downloadOriginal').href = frameUrl;
      const fields = document.getElementById('recordFields');
      fields.replaceChildren();
      const fieldSets = [
        ['Medicines', record.medicines], ['Dosages', record.dosages], ['Diagnoses mentioned', record.diagnoses],
      ];
      fieldSets.forEach(([label, values]) => {
        const section = node('div', undefined, 'detail-field');
        section.append(node('b', label), node('p', values?.length ? values.join(', ') : 'Not detected'));
        fields.append(section);
      });
      const labSection = node('div', undefined, 'detail-field');
      labSection.append(node('b', 'Laboratory values'));
      if (record.lab_values?.length) {
        const list = node('ul');
        record.lab_values.forEach((lab) => list.append(node('li', `${lab.name || 'Lab'}: ${lab.value || 'Not detected'} ${lab.unit || ''}${lab.reference_range ? ` · Reference ${lab.reference_range}` : ''}`)));
        labSection.append(list);
      } else labSection.append(node('p', 'Not detected'));
      fields.append(labSection);
      if (record.raw_text) {
        const details = node('details', undefined, 'raw-text-details');
        details.append(node('summary', 'View extracted source text'), node('pre', record.raw_text));
        fields.append(details);
      }
    } catch (error) {
      document.getElementById('recordLoading').textContent = error.message;
    }
  }

  async function wireDemoLoad() {
    const button = document.getElementById('loadDemoBtn');
    if (!button) return;
    button.addEventListener('click', async () => {
      const original = button.textContent;
      button.disabled = true;
      button.textContent = 'Loading fictional examples…';
      try {
        const data = await requestJson('/api/demo/load', { method: 'POST' });
        button.textContent = `${data.count} fictional records loaded`;
        await fillDashboard();
      } catch (error) {
        button.textContent = error.message;
      } finally {
        window.setTimeout(() => { button.disabled = false; button.textContent = original; }, 3500);
      }
    });
  }

  async function wireContactForm() {
    const form = document.getElementById('contactForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const message = document.getElementById('contactMessage');
      if (!form.reportValidity()) return;
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        const data = await requestJson('/api/contact', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.fromEntries(new FormData(form).entries())) });
        setMessage(message, data.message, 'success-message');
        form.reset();
      } catch (error) { setMessage(message, error.message, 'error-message'); }
      finally { button.disabled = false; }
    });
  }

  async function wirePasswordForm() {
    const form = document.getElementById('passwordForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const message = document.getElementById('settingsMessage');
      if (!form.reportValidity()) return;
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        await requestJson('/api/profile/password', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.fromEntries(new FormData(form).entries())) });
        setMessage(message, 'Password updated. Please log in again with your new password.', 'success-message');
        window.setTimeout(() => window.location.assign('/login'), 1500);
      } catch (error) { setMessage(message, error.message, 'error-message'); }
      finally { button.disabled = false; }
    });
  }

  async function wireProfileForm() {
    const form = document.getElementById('profileForm');
    if (!form) return;
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      const message = document.getElementById('profileMessage');
      if (!form.reportValidity()) return;
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      try {
        await requestJson('/api/profile', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(Object.fromEntries(new FormData(form).entries())),
        });
        setMessage(message, 'Profile updated.', 'success-message');
        window.setTimeout(() => window.location.reload(), 500);
      } catch (error) {
        setMessage(message, error.message, 'error-message');
      } finally {
        button.disabled = false;
      }
    });
  }

  async function fillSecurityInfo() {
    const target = document.getElementById('securityInfo');
    if (!target) return;
    try {
      const info = await requestJson('/api/security/info');
      target.replaceChildren();
      [
        ['Active sessions', String(info.active_sessions)],
        ['This session started', info.session_created_at],
        ['This session expires', info.session_expires_at],
        ['Saved documents', String(info.saved_document_count)],
        ['Session cookie', `HttpOnly · SameSite ${info.cookie_same_site}`],
      ].forEach(([label, value]) => {
        const row = node('div', undefined, 'security-row');
        row.append(node('span', label), node('b', value));
        target.append(row);
      });
    } catch (error) {
      target.replaceChildren(node('p', error.message, 'error-copy'));
    }
  }

  function wireRevokeSessions() {
    const button = document.getElementById('revokeSessionsBtn');
    if (!button) return;
    button.addEventListener('click', async () => {
      if (!window.confirm('Sign out all other sessions for this account? Your current session will remain active.')) return;
      button.disabled = true;
      const message = document.getElementById('securityMessage');
      try {
        const result = await requestJson('/api/security/revoke-other-sessions', { method: 'POST' });
        setMessage(message, `${result.revoked_sessions} other ${result.revoked_sessions === 1 ? 'session was' : 'sessions were'} signed out.`, 'success-message');
        await fillSecurityInfo();
      } catch (error) {
        setMessage(message, error.message, 'error-message');
      } finally {
        button.disabled = false;
      }
    });
  }

  fillDashboard();
  fillTimeline();
  fillRecord();
  wireDemoLoad();
  wireContactForm();
  wirePasswordForm();
  wireProfileForm();
  fillSecurityInfo();
  wireRevokeSessions();
});
