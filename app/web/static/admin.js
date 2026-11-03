// PropertyAssistant - Admin Dashboard Logic
// Vanilla JavaScript, strict DOM-based XSS safety

(function () {
  'use strict';

  const TOKEN_KEY = 'admin_token';

  // DOM elements
  const adminTokenInput = document.getElementById('admin-token');
  const btnSaveToken = document.getElementById('btn-save-token');
  const tokenStatus = document.getElementById('token-status');
  const btnLogout = document.getElementById('btn-logout');

  const btnRefresh = document.getElementById('btn-refresh');
  const checkForce = document.getElementById('check-force');
  const checkDryRun = document.getElementById('check-dry-run');

  const bannerError = document.getElementById('banner-error');
  const bannerSuccess = document.getElementById('banner-success');

  const syncSummary = document.getElementById('sync-summary');
  const statStatus = document.getElementById('stat-status');
  const statInserted = document.getElementById('stat-inserted');
  const statUpdated = document.getElementById('stat-updated');
  const statUnchanged = document.getElementById('stat-unchanged');
  const statDeactivated = document.getElementById('stat-deactivated');
  const statReactivated = document.getElementById('stat-reactivated');
  const statRejected = document.getElementById('stat-rejected');

  const runsTbody = document.getElementById('runs-tbody');
  const btnReloadRuns = document.getElementById('btn-reload-runs');

  const errorsDetail = document.getElementById('errors-detail');
  const errorsDetailTitle = document.getElementById('errors-detail-title');
  const errorsTbody = document.getElementById('errors-tbody');

  function getToken() {
    return sessionStorage.getItem(TOKEN_KEY) || '';
  }

  function setToken(token) {
    if (token) {
      sessionStorage.setItem(TOKEN_KEY, token);
      adminTokenInput.value = token;
      tokenStatus.textContent = 'Token set in session';
      tokenStatus.style.color = 'var(--success)';
    } else {
      sessionStorage.removeItem(TOKEN_KEY);
      adminTokenInput.value = '';
      tokenStatus.textContent = 'No token set';
      tokenStatus.style.color = 'var(--text-muted)';
    }
  }

  function showError(msg) {
    bannerError.textContent = msg;
    bannerError.classList.remove('hidden');
    bannerSuccess.classList.add('hidden');
  }

  function showSuccess(msg) {
    bannerSuccess.textContent = msg;
    bannerSuccess.classList.remove('hidden');
    bannerError.classList.add('hidden');
  }

  function clearBanners() {
    bannerError.classList.add('hidden');
    bannerSuccess.classList.add('hidden');
  }

  async function triggerRefresh() {
    clearBanners();
    const token = getToken();
    if (!token) {
      showError('Please provide an X-Admin-Token before triggering a refresh.');
      adminTokenInput.focus();
      return;
    }

    const force = checkForce.checked;
    const dryRun = checkDryRun.checked;

    btnRefresh.disabled = true;
    btnRefresh.textContent = 'Refreshing...';

    try {
      const response = await fetch('/admin/refresh', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Admin-Token': token,
        },
        body: JSON.stringify({
          force: force,
          dry_run: dryRun,
        }),
      });

      if (response.status === 401) {
        showError('Unauthorized (401): The provided admin token is invalid.');
        return;
      }
      if (response.status === 503) {
        showError('Service Unavailable (503): ADMIN_TOKEN is not configured on the server.');
        return;
      }
      if (!response.ok) {
        const errData = await response.json().catch(() => ({}));
        showError(`Refresh failed: ${errData.detail || response.statusText}`);
        return;
      }

      const data = await response.json();
      showSuccess(`Refresh executed successfully (${data.status}).`);

      // Populate summary
      statStatus.textContent = data.status || 'OK';
      statInserted.textContent = String(data.inserted ?? 0);
      statUpdated.textContent = String(data.updated ?? 0);
      statUnchanged.textContent = String(data.unchanged ?? 0);
      statDeactivated.textContent = String(data.deactivated ?? 0);
      statReactivated.textContent = String(data.reactivated ?? 0);
      statRejected.textContent = String(data.rejected ?? 0);
      syncSummary.classList.remove('hidden');

      // Reload runs table
      await loadRuns();
    } catch (err) {
      showError(`Network error: ${err.message}`);
    } finally {
      btnRefresh.disabled = false;
      btnRefresh.textContent = 'Refresh Inventory';
    }
  }

  async function loadRuns() {
    runsTbody.innerHTML = '';
    const loadingRow = document.createElement('tr');
    const loadingCell = document.createElement('td');
    loadingCell.colSpan = 8;
    loadingCell.textContent = 'Loading runs...';
    loadingCell.style.textAlign = 'center';
    loadingCell.style.color = 'var(--text-muted)';
    loadingRow.appendChild(loadingCell);
    runsTbody.appendChild(loadingRow);

    try {
      const token = getToken();
      const headers = token ? { 'X-Admin-Token': token } : {};
      const response = await fetch('/admin/ingestion/runs?limit=10', { headers });

      if (!response.ok) {
        runsTbody.innerHTML = '';
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 8;
        cell.textContent = `Failed to load runs: ${response.statusText}`;
        cell.style.color = 'var(--error)';
        cell.style.textAlign = 'center';
        row.appendChild(cell);
        runsTbody.appendChild(row);
        return;
      }

      const runs = await response.json();
      runsTbody.innerHTML = '';

      if (!runs || runs.length === 0) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 8;
        cell.textContent = 'No ingestion runs found.';
        cell.style.textAlign = 'center';
        cell.style.color = 'var(--text-muted)';
        row.appendChild(cell);
        runsTbody.appendChild(row);
        return;
      }

      runs.forEach(run => {
        const row = document.createElement('tr');

        // ID
        const idCell = document.createElement('td');
        idCell.textContent = String(run.id);
        row.appendChild(idCell);

        // Status
        const statusCell = document.createElement('td');
        const badge = document.createElement('span');
        badge.className = `card-badge ${run.status === 'success' ? 'badge-available' : run.status === 'skipped' ? 'badge-reserved' : 'badge-sold'}`;
        badge.textContent = run.status;
        statusCell.appendChild(badge);
        row.appendChild(statusCell);

        // Started At
        const dateCell = document.createElement('td');
        dateCell.textContent = run.started_at ? new Date(run.started_at).toLocaleString() : 'N/A';
        row.appendChild(dateCell);

        // Rows Read
        const rowsCell = document.createElement('td');
        rowsCell.textContent = String(run.rows_read ?? 0);
        row.appendChild(rowsCell);

        // Inserted / Updated
        const insUpCell = document.createElement('td');
        insUpCell.textContent = `${run.inserted ?? 0} / ${run.updated ?? 0}`;
        row.appendChild(insUpCell);

        // Rejected
        const rejCell = document.createElement('td');
        rejCell.textContent = String(run.rejected ?? 0);
        if (run.rejected > 0) {
          rejCell.style.color = 'var(--error)';
          rejCell.style.fontWeight = 'bold';
        }
        row.appendChild(rejCell);

        // Error message
        const errCell = document.createElement('td');
        errCell.textContent = run.error_message || '—';
        if (run.error_message) errCell.style.color = 'var(--error)';
        row.appendChild(errCell);

        // Actions (View errors if any)
        const actCell = document.createElement('td');
        if (run.rejected > 0) {
          const btnView = document.createElement('button');
          btnView.type = 'button';
          btnView.className = 'btn btn-outline btn-sm';
          btnView.textContent = 'View Errors';
          btnView.addEventListener('click', () => {
            loadRunErrors(run.id);
          });
          actCell.appendChild(btnView);
        } else {
          actCell.textContent = '—';
        }
        row.appendChild(actCell);

        runsTbody.appendChild(row);
      });
    } catch (err) {
      runsTbody.innerHTML = '';
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = 8;
      cell.textContent = `Error: ${err.message}`;
      cell.style.color = 'var(--error)';
      cell.style.textAlign = 'center';
      row.appendChild(cell);
      runsTbody.appendChild(row);
    }
  }

  async function loadRunErrors(runId) {
    errorsTbody.innerHTML = '';
    errorsDetail.classList.remove('hidden');
    errorsDetailTitle.textContent = `Errors for Ingestion Run #${runId}`;

    try {
      const response = await fetch(`/admin/ingestion/runs/${runId}/errors`);
      if (!response.ok) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 4;
        cell.textContent = 'Failed to load error details.';
        cell.style.color = 'var(--error)';
        row.appendChild(cell);
        errorsTbody.appendChild(row);
        return;
      }

      const errors = await response.json();
      if (!errors || errors.length === 0) {
        const row = document.createElement('tr');
        const cell = document.createElement('td');
        cell.colSpan = 4;
        cell.textContent = 'No rejection details recorded for this run.';
        row.appendChild(cell);
        errorsTbody.appendChild(row);
        return;
      }

      errors.forEach(err => {
        const row = document.createElement('tr');

        const rowCell = document.createElement('td');
        rowCell.textContent = String(err.row_number ?? 'N/A');
        row.appendChild(rowCell);

        const idCell = document.createElement('td');
        idCell.textContent = err.property_id || 'N/A';
        row.appendChild(idCell);

        const reasonCell = document.createElement('td');
        reasonCell.textContent = err.reason || 'Unknown error';
        reasonCell.style.color = 'var(--error)';
        row.appendChild(reasonCell);

        const rawCell = document.createElement('td');
        rawCell.textContent = typeof err.raw_row === 'object' ? JSON.stringify(err.raw_row) : String(err.raw_row || '');
        rawCell.style.fontFamily = 'monospace';
        rawCell.style.fontSize = '0.75rem';
        row.appendChild(rawCell);

        errorsTbody.appendChild(row);
      });
    } catch (e) {
      const row = document.createElement('tr');
      const cell = document.createElement('td');
      cell.colSpan = 4;
      cell.textContent = `Error: ${e.message}`;
      row.appendChild(cell);
      errorsTbody.appendChild(row);
    }
  }

  // --- Event Listeners ---
  btnSaveToken.addEventListener('click', () => {
    const val = adminTokenInput.value.trim();
    setToken(val);
    clearBanners();
    showSuccess('Token saved to session.');
    loadRuns();
  });

  btnLogout.addEventListener('click', () => {
    setToken('');
    clearBanners();
    showSuccess('Logged out.');
    syncSummary.classList.add('hidden');
    errorsDetail.classList.add('hidden');
    loadRuns();
  });

  btnRefresh.addEventListener('click', triggerRefresh);
  btnReloadRuns.addEventListener('click', loadRuns);

  // Initialize
  const existingToken = getToken();
  setToken(existingToken);
  loadRuns();
})();
