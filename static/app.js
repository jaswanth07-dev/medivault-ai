const state = {
  currentRecord: null,
};

const reviewBlock = document.getElementById('reviewBlock');
const reviewForm = document.getElementById('reviewForm');
const summaryBox = document.getElementById('summaryBox');
const timeline = document.getElementById('timeline');
const fileInput = document.getElementById('fileInput');
const uploadDropzone = document.getElementById('uploadDropzone');
const filePrompt = document.getElementById('filePrompt');
const selectedFile = document.getElementById('selectedFile');
const extractButton = document.getElementById('extractButton');
const uploadStatus = document.getElementById('uploadStatus');
const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
const ALLOWED_FILE_EXTENSIONS = new Set(['pdf', 'jpg', 'jpeg', 'png']);

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function setUploadStatus(message, kind = '') {
  uploadStatus.textContent = message;
  uploadStatus.className = `upload-status${kind ? ` ${kind}` : ''}`;
}

function updateSelectedFile() {
  const file = fileInput.files[0];
  if (!file) {
    selectedFile.classList.add('hidden');
    filePrompt.classList.remove('hidden');
    uploadDropzone.classList.remove('has-file');
    extractButton.disabled = true;
    setUploadStatus('');
    return;
  }

  const extension = file.name.split('.').pop().toLowerCase();
  const extensionAllowed = ALLOWED_FILE_EXTENSIONS.has(extension);
  const sizeAllowed = file.size <= MAX_UPLOAD_BYTES;
  const icon = document.getElementById('fileTypeIcon');
  icon.textContent = extension === 'pdf' ? 'PDF' : 'IMG';
  icon.classList.toggle('image', extension !== 'pdf');
  document.getElementById('selectedFileName').textContent = file.name;
  document.getElementById('selectedFileSize').textContent = formatFileSize(file.size);
  filePrompt.classList.add('hidden');
  selectedFile.classList.remove('hidden');
  uploadDropzone.classList.add('has-file');
  extractButton.disabled = !(extensionAllowed && sizeAllowed);

  if (!extensionAllowed) {
    setUploadStatus('Unsupported file type. Choose a PDF, JPG, JPEG, or PNG.', 'error');
  } else if (!sizeAllowed) {
    setUploadStatus('File is larger than the 10 MB upload limit.', 'error');
  } else {
    setUploadStatus('File selected and ready to extract.', 'success');
  }
}

function setUploadProcessing(processing) {
  const chooseFileButton = document.getElementById('chooseFileBtn');
  document.getElementById('removeFileBtn').disabled = processing;
  document.getElementById('changeFileBtn').disabled = processing;
  chooseFileButton.disabled = processing;
  extractButton.disabled = processing || !fileInput.files[0] || !isCurrentFileValid();
  extractButton.textContent = processing ? 'Extracting…' : 'Extract document';
}

function isCurrentFileValid() {
  const file = fileInput.files[0];
  if (!file) return false;
  const extension = file.name.split('.').pop().toLowerCase();
  return ALLOWED_FILE_EXTENSIONS.has(extension) && file.size <= MAX_UPLOAD_BYTES;
}

function linesToList(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value.filter(Boolean);
  return String(value)
    .split(/\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function listToText(items) {
  return Array.isArray(items) ? items.join('\n') : '';
}

function parseLabValues(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value;
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed : [];
  } catch (error) {
    return [];
  }
}

function showReviewPanel() {
  reviewBlock.classList.add('hidden');
  reviewForm.classList.remove('hidden');
}

function previewSummary(text) {
  if (!text) {
    summaryBox.textContent = 'Extracted content will appear here after upload.';
    summaryBox.classList.add('empty');
    return;
  }
  summaryBox.textContent = text;
  summaryBox.classList.remove('empty');
}

function populateReview(fields, recordMeta = {}) {
  showReviewPanel();
  document.getElementById('summaryLanguage').value = 'en';
  document.getElementById('document_date').value = fields.document_date || 'Not detected';
  document.getElementById('document_type').value = fields.document_type || 'Not detected';
  document.getElementById('medicines').value = listToText(fields.medicines && fields.medicines.length ? fields.medicines : ['Not detected']);
  document.getElementById('dosages').value = listToText(fields.dosages && fields.dosages.length ? fields.dosages : ['Not detected']);
  document.getElementById('diagnoses').value = listToText(fields.diagnoses && fields.diagnoses.length ? fields.diagnoses : ['Not detected']);
  document.getElementById('lab_values').value = JSON.stringify(fields.lab_values && fields.lab_values.length ? fields.lab_values : [{ name: 'Not detected', value: 'Not detected', unit: '', reference_range: '' }], null, 2);
  document.getElementById('summaryText').value = fields.summary || 'Not detected';
  previewSummary(fields.summary || 'Not detected');
  state.currentRecord = {
    ...recordMeta,
    ...fields,
  };
}

async function updateSummaryLanguage() {
  if (!state.currentRecord) return;
  const fields = {
    document_type: document.getElementById('document_type').value.trim() || 'medical document',
    document_date: document.getElementById('document_date').value.trim() || 'Not detected',
    diagnoses: linesToList(document.getElementById('diagnoses').value).filter((item) => item !== 'Not detected'),
    medicines: linesToList(document.getElementById('medicines').value).filter((item) => item !== 'Not detected'),
    lab_values: parseLabValues(document.getElementById('lab_values').value),
  };
  const response = await fetch('/api/summary', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ language: document.getElementById('summaryLanguage').value, fields }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Summary could not be generated.');
  document.getElementById('summaryText').value = data.summary;
  previewSummary(data.summary);
}

async function loadTimeline() {
  if (!timeline) return;
  try {
    const response = await fetch('/api/timeline');
    const data = await response.json();
    const records = data.records || [];

    if (!records.length) {
      timeline.innerHTML = '<div class="timeline-card">No records saved yet.</div>';
      return;
    }

    timeline.innerHTML = records
      .map((record) => {
        const meds = linesToList(record.medicines || []).join(', ') || 'No medicines listed';
        const diagnoses = linesToList(record.diagnoses || []).join(', ') || 'No diagnoses listed';
        const labs = (record.lab_values || [])
          .map((item) => `${item.name || 'Lab'} ${item.value || ''}${item.unit || ''}`)
          .join(', ') || 'No lab values listed';

        const tagClass = record.is_synthetic ? 'synthetic' : 'original';
        const tagText = record.is_synthetic ? 'Synthetic' : 'Original';

        return `
          <article class="timeline-card">
            <div class="timeline-card-header">
              <div>
                <h3>${record.document_type || 'Medical document'}</h3>
                <div>${record.document_date || 'Date not stated'}</div>
              </div>
              <span class="tag ${tagClass}">${tagText}</span>
            </div>
            <p>${record.summary || 'No summary available.'}</p>
            <ul class="timeline-list">
              <li><strong>Medications:</strong> ${meds}</li>
              <li><strong>Diagnoses:</strong> ${diagnoses}</li>
              <li><strong>Lab values:</strong> ${labs}</li>
            </ul>
          </article>
        `;
      })
      .join('');
  } catch (error) {
    timeline.innerHTML = '<div class="timeline-card">Unable to load timeline data.</div>';
  }
}

async function handleUpload(event) {
  event.preventDefault();
  const file = fileInput.files[0];

  if (!file) {
    setUploadStatus('Choose a PDF, JPG, JPEG, or PNG file to continue.', 'error');
    return;
  }
  if (!isCurrentFileValid()) {
    updateSelectedFile();
    return;
  }

  const formData = new FormData();
  formData.append('file', file);
  setUploadProcessing(true);
  setUploadStatus('Uploading and extracting document…', 'loading');

  try {
    const response = await fetch('/api/upload', {
      method: 'POST',
      body: formData,
    });

    if (!response.ok) {
      let message = 'Upload failed. Please try again.';
      try {
        const errorData = await response.json();
        message = errorData.detail || message;
      } catch (parseError) {
        // Keep the user-facing fallback for non-JSON server errors.
      }
      throw new Error(message);
    }

    const data = await response.json();
    populateReview(data.fields, {
      filename: data.filename,
      stored_path: data.stored_path,
      source_type: data.source_type,
      is_synthetic: data.is_synthetic,
      raw_text: data.extracted_text || '',
    });
    previewSummary(data.summary || '');
    setUploadStatus(`Successfully extracted ${data.filename}. Review the fields before saving.`, 'success');
  } catch (error) {
    setUploadStatus(error.message || 'The document could not be processed.', 'error');
  } finally {
    setUploadProcessing(false);
  }
}

async function saveRecord() {
  if (!state.currentRecord) {
    alert('No record is ready to save.');
    return;
  }

  const payload = {
    filename: state.currentRecord.filename || 'document',
    stored_path: state.currentRecord.stored_path || '',
    source_type: state.currentRecord.source_type || 'unknown',
    is_synthetic: !!state.currentRecord.is_synthetic,
    raw_text: state.currentRecord.raw_text || '',
    document_date: document.getElementById('document_date').value.trim() || 'Not detected',
    document_type: document.getElementById('document_type').value.trim() || 'Not detected',
    medicines: linesToList(document.getElementById('medicines').value).filter((item) => item !== 'Not detected'),
    dosages: linesToList(document.getElementById('dosages').value).filter((item) => item !== 'Not detected'),
    diagnoses: linesToList(document.getElementById('diagnoses').value).filter((item) => item !== 'Not detected'),
    lab_values: parseLabValues(document.getElementById('lab_values').value).filter((item) => !(item.name === 'Not detected' && item.value === 'Not detected')),
    summary: document.getElementById('summaryText').value.trim() || 'Not detected',
  };

  try {
    const response = await fetch('/api/save', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(payload),
    });

    if (!response.ok) {
      const errorData = await response.json();
      throw new Error(errorData.detail || 'Save failed.');
    }

    await loadTimeline();
    setUploadStatus('Record saved to your health timeline.', 'success');
  } catch (error) {
    setUploadStatus(error.message || 'The record could not be saved.', 'error');
  }
}

async function handleDemoLoad() {
  try {
    const response = await fetch('/api/demo/load', { method: 'POST' });
    if (!response.ok) {
      throw new Error('Demo records could not be loaded.');
    }
    const data = await response.json();
    if (data.records && data.records.length) {
      const record = data.records[data.records.length - 1];
      populateReview(record, {
        filename: record.filename,
        stored_path: record.stored_path,
        source_type: record.source_type,
        is_synthetic: record.is_synthetic,
        raw_text: record.raw_text || '',
      });
      previewSummary(record.summary || '');
    }
    await loadTimeline();
  } catch (error) {
    alert(error.message || 'Demo data could not be loaded.');
  }
}

document.getElementById('uploadForm').addEventListener('submit', handleUpload);
document.getElementById('chooseFileBtn').addEventListener('click', () => fileInput.click());
document.getElementById('changeFileBtn').addEventListener('click', () => fileInput.click());
document.getElementById('removeFileBtn').addEventListener('click', () => {
  fileInput.value = '';
  updateSelectedFile();
});
fileInput.addEventListener('change', updateSelectedFile);
document.getElementById('saveRecordBtn')?.addEventListener('click', saveRecord);
document.getElementById('summaryLanguage')?.addEventListener('change', () => {
  updateSummaryLanguage().catch((error) => setUploadStatus(error.message, 'error'));
});
document.getElementById('demoLoadBtn')?.addEventListener('click', handleDemoLoad);

loadTimeline();
