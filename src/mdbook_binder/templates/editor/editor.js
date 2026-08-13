/* ===== Book-binder Editor JS (forked from Lecture_forge, lecture-forge non-dependent) ===== */
'use strict';

// ------------------------------------------------------------------ //
//  State                                                               //
// ------------------------------------------------------------------ //
let currentSectionId = null;
let currentSectionIndex = -1;       // index into lectureData.sections
let mde = null;                     // EasyMDE instance
let currentTab = 'edit';
let currentGalleryTab = 'recommended';
let galleryPage = 1;
let selectedImagePath = null;       // path chosen in modal
let activeReplaceImgIdx = null;     // image_editor img_index to replace (replace mode)
let uploadedFilePath = null;        // path returned from /api/images/upload
let lectureData = null;             // cached from /api/lecture
let elementsData = [];              // cached from /api/elements
let galleryMode = 'replace';        // 'replace' | 'add'
let _pendingAdditions = {};         // section_id -> [{path, caption, thumbnail}]
let _galleryPaths = [];             // [{path, label}] for current gallery/recommended grid

// ------------------------------------------------------------------ //
//  Init                                                                //
// ------------------------------------------------------------------ //
document.addEventListener('DOMContentLoaded', async () => {
  if (typeof mermaid !== 'undefined') {
    // HTML/PDF 빌드(mermaid_prerender.py)와 같은 폰트를 지정한다 — 지정하지 않으면
    // Mermaid 기본 폰트("trebuchet ms" 등, 한글 글리프 없음)로 라벨 크기를 계산해
    // 놓고 브라우저는 폴백 한글 폰트로 그리면서 실제 글자 폭이 계산치보다 커져,
    // 서브그래프 라벨이 자기 박스 밖으로 넘치고 좁은 미리보기에서 잘려 보인다.
    mermaid.initialize({
      startOnLoad: false, theme: 'default', securityLevel: 'loose',
      themeVariables: { fontFamily: '"Noto Sans KR", sans-serif' },
    });
  }
  await loadLecture();
  await loadElements();
  initMDE();
  bindSaveAll();
  bindExit();
  bindSaveSection();
  bindGallerySearch();
  bindUploadDrop();
});

// ------------------------------------------------------------------ //
//  Lecture load                                                        //
// ------------------------------------------------------------------ //
async function loadLecture() {
  try {
    const res = await fetch('/api/lecture');
    lectureData = await res.json();
    renderSectionList(lectureData.sections);
  } catch (e) {
    showToast('도서 로드 실패: ' + e.message, 'error');
  }
}

function renderSectionList(sections) {
  const list = document.getElementById('section-list');
  if (!sections || sections.length === 0) {
    list.innerHTML = '<div class="text-center text-gray-400 text-xs py-4">섹션 없음</div>';
    return;
  }

  list.innerHTML = sections.map((sec, idx) => {
    const isActive = idx === currentSectionIndex;
    const isDeleted = sec.status === 'deleted';
    const isModified = sec.status === 'modified';
    let badge = '';
    if (isDeleted) badge = '<span class="section-badge badge-deleted">삭제</span>';
    else if (isModified) badge = '<span class="section-badge badge-modified">수정됨</span>';

    return `
      <div class="section-item ${isActive ? 'active' : ''} ${isDeleted ? 'deleted' : ''} ${isModified ? 'modified' : ''}"
           data-id="${sec.id}" data-index="${idx}" onclick="selectSection('${sec.id}', ${idx})">
        <div class="flex-1 min-w-0">
          <div class="section-title">${escHtml(sec.title)}</div>
          <div class="section-meta">${sec.word_count}단어 · 이미지 ${sec.image_count} · 다이어그램 ${sec.diagram_count}</div>
        </div>
        ${badge}
        ${isDeleted
          ? `<button class="btn-delete-section text-green-500" title="삭제 취소" onclick="event.stopPropagation(); restoreSection('${sec.id}')">↩</button>`
          : `<button class="btn-delete-section" title="섹션 삭제" onclick="event.stopPropagation(); confirmDeleteSection('${sec.id}')">🗑</button>`
        }
      </div>`;
  }).join('');
}

// ------------------------------------------------------------------ //
//  Section selection                                                   //
// ------------------------------------------------------------------ //
async function selectSection(sectionId, sectionIndex) {
  currentSectionId = sectionId;
  if (sectionIndex !== undefined) currentSectionIndex = sectionIndex;

  // Highlight in list
  document.querySelectorAll('.section-item').forEach(el => {
    el.classList.toggle('active', parseInt(el.dataset.index) === currentSectionIndex);
  });

  // Load section content
  try {
    const res = await fetch(`/api/sections/${encodeURIComponent(sectionId)}`);
    const data = await res.json();
    if (data.error) { showToast(data.error, 'error'); return; }

    showEditorPane();
    mde.value(data.markdown || '');
    updateWordCount(data.markdown || '');

    // Update section title input if available
    const titleInput = document.getElementById('section-title-input');
    if (titleInput) titleInput.value = data.title || '';
  } catch (e) {
    showToast('섹션 로드 실패: ' + e.message, 'error');
  }

  // Update visuals panel for this section
  renderVisualsForSection(sectionId);
  // Sync pending additions from server (in case page was refreshed)
  _syncPendingAdditions(sectionId);
}

function showEditorPane() {
  document.getElementById('editor-empty').classList.add('hidden');
  document.getElementById('editor-pane').classList.remove('hidden');
  document.getElementById('save-section-bar').classList.remove('hidden');
  if (currentTab === 'preview') {
    document.getElementById('preview-pane').classList.remove('hidden');
  }
}

// ------------------------------------------------------------------ //
//  EasyMDE init                                                        //
// ------------------------------------------------------------------ //
function initMDE() {
  mde = new EasyMDE({
    element: document.getElementById('mde-editor'),
    autofocus: false,
    spellChecker: false,
    // index.html이 이미 로컬 번들 Font Awesome을 로드한다 — 끄지 않으면
    // EasyMDE가 href로 판별을 못 해 CDN에서 또 하나를 주입하려 시도한다
    // (CDN 차단 환경에서 실패 요청만 남고, 오프라인 목표에도 어긋난다).
    autoDownloadFontAwesome: false,
    toolbar: [
      'bold', 'italic', 'heading', '|',
      'unordered-list', 'ordered-list', '|',
      'code', 'quote', 'horizontal-rule', '|',
      'preview', 'guide'
    ],
    minHeight: '200px',
    renderingConfig: { singleLineBreaks: true },
    status: false,
  });

  mde.codemirror.on('change', () => {
    updateWordCount(mde.value());
    if (currentTab === 'preview') renderPreview();
  });
}

function updateWordCount(text) {
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  const el = document.getElementById('word-count');
  if (el) el.textContent = `단어수: ${words}`;
}

// ------------------------------------------------------------------ //
//  Tabs                                                                //
// ------------------------------------------------------------------ //
function switchTab(tab) {
  currentTab = tab;
  const editBtn = document.getElementById('tab-edit');
  const previewBtn = document.getElementById('tab-preview');
  const editorPane = document.getElementById('editor-pane');
  const previewPane = document.getElementById('preview-pane');

  if (tab === 'edit') {
    editBtn.classList.add('active');
    previewBtn.classList.remove('active');
    if (currentSectionId) {
      editorPane.classList.remove('hidden');
    }
    previewPane.classList.add('hidden');
  } else {
    previewBtn.classList.add('active');
    editBtn.classList.remove('active');
    editorPane.classList.add('hidden');
    previewPane.classList.remove('hidden');
    renderPreview();
  }
}

function renderPreview() {
  const pane = document.getElementById('preview-pane');
  const md = mde ? mde.value() : '';
  pane.innerHTML = marked.parse(md);
}

// ------------------------------------------------------------------ //
//  Save section                                                        //
// ------------------------------------------------------------------ //
function bindSaveSection() {
  document.getElementById('btn-save-section').addEventListener('click', saveCurrentSection);
}

async function saveCurrentSection() {
  if (!currentSectionId) { showToast('섹션을 먼저 선택하세요', 'info'); return; }
  const md = mde.value();
  try {
    const res = await fetch(`/api/sections/${encodeURIComponent(currentSectionId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ markdown: md }),
    });
    const data = await res.json();
    if (!data.success) { showToast(data.error || '저장 실패', 'error'); return; }
    showToast('섹션 저장됨 (미리 스테이징)', 'success');
    // Refresh section list to show "modified" badge
    await loadLecture();
  } catch (e) {
    showToast('저장 실패: ' + e.message, 'error');
  }
}

// ------------------------------------------------------------------ //
//  Delete / restore section                                            //
// ------------------------------------------------------------------ //
async function confirmDeleteSection(sectionId) {
  if (!confirm('이 섹션을 삭제하시겠습니까? (저장 전까지 취소 가능)')) return;
  await deleteSection(sectionId);
}

async function deleteSection(sectionId) {
  try {
    const res = await fetch(`/api/sections/${encodeURIComponent(sectionId)}`, { method: 'DELETE' });
    const data = await res.json();
    if (!res.ok) { showToast(data.error || '삭제 실패', 'error'); return; }
    showToast('섹션 삭제 표시됨 (저장 시 반영)', 'info');
    if (currentSectionId === sectionId) {
      currentSectionId = null;
      document.getElementById('editor-empty').classList.remove('hidden');
      document.getElementById('editor-pane').classList.add('hidden');
      document.getElementById('preview-pane').classList.add('hidden');
      document.getElementById('save-section-bar').classList.add('hidden');
    }
    await loadLecture();
  } catch (e) {
    showToast('삭제 실패: ' + e.message, 'error');
  }
}

async function restoreSection(sectionId) {
  // Restoring means re-staging as "original" — send empty markdown to unstage.
  // Simplest: reload the page's server state by sending a POST with the current HTML content.
  // For simplicity here we tell the user to reload if they want to fully undo.
  showToast('새로고침하여 삭제를 취소할 수 있습니다 (저장 전)', 'info');
}

// ------------------------------------------------------------------ //
//  Visual elements                                                     //
// ------------------------------------------------------------------ //
async function loadElements() {
  try {
    const res = await fetch('/api/elements');
    const data = await res.json();
    elementsData = data.elements || [];
  } catch (e) {
    console.warn('elements load failed', e);
  }
}

function renderVisualsForSection(sectionId) {
  const panel = document.getElementById('visuals-list');

  // Show "이미지 추가" button only when a section is selected
  const addBtn = document.getElementById('btn-add-image');
  if (addBtn) addBtn.classList.toggle('hidden', !sectionId);

  if (!sectionId) {
    panel.innerHTML = '<div class="text-center text-gray-400 text-xs py-4">섹션을 선택하면 이미지/다이어그램이 표시됩니다</div>';
    return;
  }

  // Find section title for matching — use index to avoid duplicate-id false matches
  const sec = lectureData && (
    currentSectionIndex >= 0
      ? lectureData.sections[currentSectionIndex]
      : lectureData.sections.find(s => s.id === sectionId)
  );
  const secTitle = sec ? sec.title : '';

  // Filter elements belonging to this section
  const sectionElements = elementsData.filter(el => {
    if (!secTitle) return false;
    return el.section && el.section.includes(secTitle.replace(/^\d+\.\s*/, '').substring(0, 20));
  });

  // Pending additions for this section
  const pending = _pendingAdditions[sectionId] || [];

  const existingHtml = sectionElements.map(el => renderVisualCard(el)).join('');
  const pendingHtml  = pending.map((item, i) => renderPendingCard(item, sectionId, i)).join('');
  const emptyNote    = (sectionElements.length === 0 && pending.length === 0)
    ? '<div class="text-center text-gray-400 text-xs py-4">이 섹션에 비주얼 없음</div>' : '';

  panel.innerHTML = emptyNote + existingHtml + pendingHtml;
  _renderDiagramPreviews();
}

function renderPendingCard(item, sectionId, index) {
  const thumb = item.thumbnail
    ? `<img class="visual-card-thumb-img" src="${item.thumbnail}" alt="${escAttr(item.caption || '추가 이미지')}" style="cursor:default">`
    : `<div class="visual-card-thumb mermaid-thumb">[추가 이미지]</div>`;
  return `
    <div class="visual-card" style="border-color:#86efac">
      ${thumb}
      <div class="visual-card-body">
        <div class="visual-card-title" style="color:#15803d">✚ 추가 예정</div>
        <div class="visual-card-meta">${escHtml(item.caption || '캡션 없음')}</div>
      </div>
      <div class="visual-card-actions">
        <button class="btn-vcard-delete" onclick="removePendingAddition('${escAttr(sectionId)}', ${index})">✕ 취소</button>
      </div>
    </div>`;
}

function renderVisualCard(el) {
  const isDeleted = el.status === 'delete';
  const kind = el.kind;
  const dispIdx = el.display_index;

  let thumbHtml = '';
  if (kind === 'image') {
    // Add cache-buster when a replacement is staged so the browser fetches the new image
    const cb = el.status === 'replace' ? `?v=${Date.now()}` : '';
    const imgSrc = `/api/images/by-index/${el.img_index}${cb}`;
    thumbHtml = `<img class="visual-card-thumb-img" src="${imgSrc}" alt="${escAttr(el.title || '')}"
      title="클릭하여 크게 보기"
      onclick="openLightbox(${el.img_index})"
      onerror="this.outerHTML='<div class=\\'visual-card-thumb mermaid-thumb\\'>[이미지 ${dispIdx}]</div>'"
      loading="lazy">`;
  } else {
    const dgmCode = el.mermaid_code || '';
    thumbHtml = `
      <div class="dgm-thumb-wrapper" title="클릭하여 크게 보기"
           onclick="openDiagramModal(${el.dgm_index})">
        <div class="dgm-thumb-inner">
          <div class="mermaid">${dgmCode}</div>
        </div>
      </div>`;
  }

  let actions = '';
  if (isDeleted) {
    actions = `<button class="btn-vcard-restore" onclick="toggleElement(${dispIdx}, 'undelete')">↩ 복원</button>`;
  } else {
    actions = `<button class="btn-vcard-delete" onclick="toggleElement(${dispIdx}, 'delete')">🗑 삭제</button>`;
    if (kind === 'image') {
      actions += `<button class="btn-vcard-replace" onclick="openGalleryModal(${el.img_index})">🔄 교체</button>`;
    }
  }

  return `
    <div class="visual-card ${isDeleted ? 'deleted' : ''}" id="vcard-${dispIdx}">
      ${thumbHtml}
      <div class="visual-card-body">
        <div class="visual-card-title">${escHtml(el.title || '')}</div>
        <div class="visual-card-meta">${el.extra || ''} · ${escHtml(el.section || '').substring(0, 20)}</div>
      </div>
      <div class="visual-card-actions">${actions}</div>
    </div>`;
}

async function toggleElement(displayIdx, action) {
  try {
    const res = await fetch(`/api/elements/${displayIdx}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action }),
    });
    const data = await res.json();
    if (data.elements) {
      elementsData = data.elements;
      renderVisualsForSection(currentSectionId);
      showToast(action === 'delete' ? '삭제 표시됨' : '삭제 취소됨', 'info');
    }
  } catch (e) {
    showToast('작업 실패: ' + e.message, 'error');
  }
}

// ------------------------------------------------------------------ //
//  Gallery modal                                                       //
// ------------------------------------------------------------------ //
function openGalleryModal(imgIdx) {
  galleryMode = 'replace';
  activeReplaceImgIdx = imgIdx;
  selectedImagePath = null;
  uploadedFilePath = null;
  _setGalleryModalMode('replace');
  document.getElementById('btn-confirm-replace').disabled = true;
  document.getElementById('gallery-modal').classList.remove('hidden');
  switchGalleryTab('recommended');
  loadRecommendedImages(imgIdx);
}

function openAddImageModal() {
  if (!currentSectionId) { showToast('섹션을 먼저 선택하세요', 'info'); return; }
  galleryMode = 'add';
  activeReplaceImgIdx = null;
  selectedImagePath = null;
  uploadedFilePath = null;
  _setGalleryModalMode('add');
  document.getElementById('btn-confirm-replace').disabled = true;
  document.getElementById('gallery-modal').classList.remove('hidden');
  // Skip "recommended" in add mode — go straight to gallery
  switchGalleryTab('gallery');
}

function _setGalleryModalMode(mode) {
  const btn = document.getElementById('btn-confirm-replace');
  const captionRow = document.getElementById('caption-row');
  const recTab = document.getElementById('gtab-recommended');
  if (mode === 'add') {
    btn.textContent = '이미지 추가';
    captionRow.classList.remove('hidden');
    recTab.classList.add('hidden');
    document.getElementById('add-caption-input').value = '';
  } else {
    btn.textContent = '선택한 이미지 교체';
    captionRow.classList.add('hidden');
    recTab.classList.remove('hidden');
  }
}

function closeGalleryModal() {
  document.getElementById('gallery-modal').classList.add('hidden');
  activeReplaceImgIdx = null;
  selectedImagePath = null;
  uploadedFilePath = null;
}

function switchGalleryTab(tab) {
  currentGalleryTab = tab;
  ['recommended', 'gallery', 'upload'].forEach(t => {
    document.getElementById(`gtab-${t}`).classList.toggle('active', t === tab);
    document.getElementById(`gpane-${t}`).classList.toggle('hidden', t !== tab);
  });
  if (tab === 'gallery') loadGallery();
}

async function loadRecommendedImages(imgIdx) {
  const grid = document.getElementById('recommended-grid');
  const empty = document.getElementById('recommended-empty');
  grid.innerHTML = '<div class="col-span-4 text-center text-gray-400 text-sm py-4">검색 중...</div>';
  empty.classList.add('hidden');

  try {
    const res = await fetch(`/api/images/${imgIdx}/alternatives`);
    const data = await res.json();
    const alts = data.alternatives || [];

    if (alts.length === 0) {
      grid.innerHTML = '';
      empty.classList.remove('hidden');
      return;
    }

    _galleryPaths = alts.map(a => ({ path: a.path, label: a.description || '' }));
    grid.innerHTML = alts.map((alt, i) => `
      <div class="gallery-item" data-path="${escAttr(alt.path)}" onclick="selectGalleryImage(this, '${escAttr(alt.path)}')">
        ${alt.thumbnail
          ? `<img src="${alt.thumbnail}" alt="${escAttr(alt.description || '')}">`
          : `<div class="flex items-center justify-center h-full text-xs text-gray-400">이미지</div>`}
        ${alt.page ? `<span class="page-badge">p.${alt.page}</span>` : ''}
        <button class="gallery-zoom-btn" title="크게 보기"
                onclick="event.stopPropagation(); openGalleryPreview(${i})">🔍</button>
      </div>
    `).join('');
  } catch (e) {
    grid.innerHTML = `<div class="col-span-4 text-center text-red-400 text-sm py-4">오류: ${e.message}</div>`;
  }
}

async function loadGallery() {
  const grid = document.getElementById('gallery-grid');
  const empty = document.getElementById('gallery-empty');
  const q = document.getElementById('gallery-search').value.trim();

  grid.innerHTML = '<div class="col-span-4 text-center text-gray-400 text-sm py-4">로딩 중...</div>';
  empty.classList.add('hidden');

  try {
    const url = `/api/gallery?page=${galleryPage}&per_page=24${q ? '&q=' + encodeURIComponent(q) : ''}`;
    const res = await fetch(url);
    const data = await res.json();
    const images = data.images || [];

    if (images.length === 0) {
      grid.innerHTML = '';
      empty.classList.remove('hidden');
      renderGalleryPager(data);
      return;
    }

    _galleryPaths = images.map(img => ({ path: img.path, label: img.name }));
    grid.innerHTML = images.map((img, i) => `
      <div class="gallery-item" data-path="${escAttr(img.path)}" onclick="selectGalleryImage(this, '${escAttr(img.path)}')">
        ${img.thumbnail
          ? `<img src="${img.thumbnail}" alt="${escAttr(img.name)}">`
          : `<div class="flex items-center justify-center h-full text-xs text-gray-400">${escHtml(img.name)}</div>`}
        <button class="gallery-zoom-btn" title="크게 보기"
                onclick="event.stopPropagation(); openGalleryPreview(${i})">🔍</button>
      </div>
    `).join('');

    renderGalleryPager(data);
  } catch (e) {
    grid.innerHTML = `<div class="col-span-4 text-center text-red-400 text-sm py-4">오류: ${e.message}</div>`;
  }
}

function renderGalleryPager(data) {
  const pager = document.getElementById('gallery-pager');
  if (!data || data.pages <= 1) { pager.innerHTML = ''; return; }
  let html = '';
  for (let i = 1; i <= data.pages; i++) {
    html += `<button onclick="setGalleryPage(${i})"
      class="px-2 py-1 text-xs rounded ${i === galleryPage ? 'bg-blue-600 text-white' : 'bg-gray-200 hover:bg-gray-300'}">${i}</button>`;
  }
  pager.innerHTML = html;
}

function setGalleryPage(page) {
  galleryPage = page;
  loadGallery();
}

function selectGalleryImage(el, path) {
  document.querySelectorAll('.gallery-item').forEach(i => i.classList.remove('selected'));
  el.classList.add('selected');
  selectedImagePath = path;
  uploadedFilePath = null;
  document.getElementById('btn-confirm-replace').disabled = false;
}

function bindGallerySearch() {
  const input = document.getElementById('gallery-search');
  if (input) {
    input.addEventListener('keydown', e => { if (e.key === 'Enter') { galleryPage = 1; loadGallery(); } });
  }
}

// ------------------------------------------------------------------ //
//  Upload                                                              //
// ------------------------------------------------------------------ //
function bindUploadDrop() {
  const dz = document.getElementById('upload-dropzone');
  if (!dz) return;
  dz.addEventListener('dragover', e => { e.preventDefault(); dz.classList.add('drag-over'); });
  dz.addEventListener('dragleave', () => dz.classList.remove('drag-over'));
  dz.addEventListener('drop', e => {
    e.preventDefault();
    dz.classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (file) uploadFile(file);
  });
}

function handleUpload(event) {
  const file = event.target.files[0];
  if (file) uploadFile(file);
}

async function uploadFile(file) {
  const formData = new FormData();
  formData.append('file', file);
  try {
    const res = await fetch('/api/images/upload', { method: 'POST', body: formData });
    const data = await res.json();
    if (!data.success) { showToast(data.error || '업로드 실패', 'error'); return; }

    uploadedFilePath = data.path;
    selectedImagePath = null;

    // Show preview
    const preview = document.getElementById('upload-preview');
    const img = document.getElementById('upload-preview-img');
    const name = document.getElementById('upload-preview-name');
    preview.classList.remove('hidden');
    if (data.thumbnail) img.src = data.thumbnail;
    name.textContent = data.name;

    document.getElementById('btn-confirm-replace').disabled = false;
    showToast('업로드 완료', 'success');
  } catch (e) {
    showToast('업로드 실패: ' + e.message, 'error');
  }
}

// ------------------------------------------------------------------ //
//  Confirm image selection (replace OR add)                            //
// ------------------------------------------------------------------ //
async function confirmImageSelection() {
  const path = uploadedFilePath || selectedImagePath;
  if (!path) { showToast('이미지를 먼저 선택하세요', 'info'); return; }

  if (galleryMode === 'replace') {
    await _doReplaceImage(path);
  } else {
    await _doAddImage(path);
  }
}

async function _doReplaceImage(path) {
  if (!activeReplaceImgIdx) return;
  try {
    const res = await fetch(`/api/images/${activeReplaceImgIdx}/replace`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path }),
    });
    const data = await res.json();
    if (!data.success) { showToast(data.error || '교체 실패', 'error'); return; }
    showToast('이미지 교체 예정 (저장 시 반영)', 'success');
    closeGalleryModal();
    await loadElements();
    if (currentSectionId) renderVisualsForSection(currentSectionId);
  } catch (e) {
    showToast('교체 실패: ' + e.message, 'error');
  }
}

async function _doAddImage(path) {
  const caption = (document.getElementById('add-caption-input').value || '').trim();
  try {
    const res = await fetch(`/api/sections/${encodeURIComponent(currentSectionId)}/images`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, caption }),
    });
    const data = await res.json();
    if (!data.success) { showToast(data.error || '추가 실패', 'error'); return; }

    // Update local pending state immediately (no round-trip needed)
    if (!_pendingAdditions[currentSectionId]) _pendingAdditions[currentSectionId] = [];
    _pendingAdditions[currentSectionId].push({ path, caption, thumbnail: data.thumbnail || '' });

    showToast('이미지 추가 예정 (저장 시 반영)', 'success');
    closeGalleryModal();
    renderVisualsForSection(currentSectionId);
  } catch (e) {
    showToast('추가 실패: ' + e.message, 'error');
  }
}

async function removePendingAddition(sectionId, index) {
  try {
    await fetch(`/api/sections/${encodeURIComponent(sectionId)}/images/${index}`, { method: 'DELETE' });
    const arr = _pendingAdditions[sectionId];
    if (arr) arr.splice(index, 1);
    renderVisualsForSection(sectionId);
    showToast('추가 취소됨', 'info');
  } catch (e) {
    showToast('취소 실패: ' + e.message, 'error');
  }
}

// ------------------------------------------------------------------ //
//  Save all                                                            //
// ------------------------------------------------------------------ //
function bindSaveAll() {
  document.getElementById('btn-save-all').addEventListener('click', saveAll);
}

async function saveAll() {
  const btn = document.getElementById('btn-save-all');
  const status = document.getElementById('save-status');
  btn.disabled = true;
  btn.textContent = '저장 중...';
  status.textContent = '';

  try {
    const res = await fetch('/api/save', { method: 'POST' });
    const data = await res.json();
    if (!data.success) { showToast(data.error || '저장 실패', 'error'); return; }
    showToast('✅ 저장 완료: ' + data.path, 'success');
    status.textContent = '저장됨 → ' + (data.path || '');
  } catch (e) {
    showToast('저장 실패: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '💾 저장';
  }
}

// ------------------------------------------------------------------ //
//  Exit                                                                //
// ------------------------------------------------------------------ //
function bindExit() {
  document.getElementById('btn-exit').addEventListener('click', async () => {
    if (!confirm('편집기를 종료하시겠습니까?\n저장하지 않은 변경사항은 사라집니다.')) return;
    try {
      await fetch('/api/shutdown', { method: 'POST' });
    } catch (_) { /* server may stop before responding */ }
    document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;font-family:sans-serif;color:#374151"><div><h2>편집기 종료됨</h2><p style="color:#9ca3af">이 탭을 닫으세요.</p></div></div>';
  });
}

// ------------------------------------------------------------------ //
//  Toast                                                               //
// ------------------------------------------------------------------ //
let _toastTimer = null;
function showToast(msg, type = 'info') {
  const wrap = document.getElementById('toast');
  const inner = document.getElementById('toast-inner');
  inner.textContent = msg;
  inner.className = type;
  wrap.classList.remove('hidden');
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => wrap.classList.add('hidden'), 3500);
}

// ------------------------------------------------------------------ //
//  Pending additions sync                                              //
// ------------------------------------------------------------------ //
async function _syncPendingAdditions(sectionId) {
  // Only fetch if we don't already have local state (avoids overwriting in-memory)
  if (_pendingAdditions[sectionId]) return;
  try {
    const res = await fetch(`/api/sections/${encodeURIComponent(sectionId)}/images`);
    const data = await res.json();
    if (data.additions && data.additions.length > 0) {
      _pendingAdditions[sectionId] = data.additions.map(a => ({
        path: a.path, caption: a.caption, thumbnail: a.thumbnail,
      }));
      renderVisualsForSection(sectionId);
    }
  } catch (_) { /* non-critical */ }
}

// ------------------------------------------------------------------ //
//  Lightbox                                                            //
// ------------------------------------------------------------------ //
// _lbSources: [{type:'index', value:imgIndex, label} | {type:'path', value:filePath, label}]
let _lbSources = [];
let _lbCurrent = 0;

/** Open lightbox for a section image (by img_index). */
function openLightbox(imgIndex) {
  const secElements = elementsData.filter(el => {
    if (el.kind !== 'image') return false;
    if (!currentSectionId) return true;
    const sec = lectureData && lectureData.sections.find(s => s.id === currentSectionId);
    if (!sec) return true;
    const title = sec.title.replace(/^\d+\.\s*/, '').substring(0, 20);
    return el.section && el.section.includes(title);
  });

  _lbSources = secElements.map(el => ({ type: 'index', value: el.img_index, label: el.title || '', replaced: el.status === 'replace' }));
  if (_lbSources.length === 0) _lbSources = [{ type: 'index', value: imgIndex, label: '', replaced: false }];
  _lbCurrent = Math.max(0, _lbSources.findIndex(s => s.type === 'index' && s.value === imgIndex));

  _openLightbox();
}

/** Open lightbox for a gallery path (by index into _galleryPaths). */
function openGalleryPreview(pathIdx) {
  if (_galleryPaths.length === 0) return;
  _lbSources = _galleryPaths.map(p => ({ type: 'path', value: p.path, label: p.label || '' }));
  _lbCurrent = Math.max(0, Math.min(pathIdx, _lbSources.length - 1));
  _openLightbox();
}

function _openLightbox() {
  _renderLightbox();
  document.getElementById('lightbox').classList.remove('hidden');
  document.addEventListener('keydown', _lbKeyHandler);
}

function _renderLightbox() {
  const src = _lbSources[_lbCurrent];
  const imgSrc = src.type === 'index'
    ? `/api/images/by-index/${src.value}${src.replaced ? `?v=${Date.now()}` : ''}`
    : `/api/images/serve?path=${encodeURIComponent(src.value)}`;
  const caption = src.label || '';

  document.getElementById('lb-img').src = imgSrc;
  document.getElementById('lb-img').alt = caption;
  document.getElementById('lb-caption').textContent = caption;
  document.getElementById('lb-counter').textContent =
    _lbSources.length > 1 ? `${_lbCurrent + 1} / ${_lbSources.length}` : '';

  document.getElementById('lb-prev').style.visibility = _lbCurrent > 0 ? 'visible' : 'hidden';
  document.getElementById('lb-next').style.visibility = _lbCurrent < _lbSources.length - 1 ? 'visible' : 'hidden';
}

function lightboxStep(dir) {
  const next = _lbCurrent + dir;
  if (next < 0 || next >= _lbSources.length) return;
  _lbCurrent = next;
  _renderLightbox();
}

function closeLightbox(event) {
  // Only close when clicking the backdrop or close button (not the img/arrows)
  if (event && event.target !== document.getElementById('lightbox')) return;
  _doCloseLightbox();
}

function _doCloseLightbox() {
  document.getElementById('lightbox').classList.add('hidden');
  document.getElementById('lb-img').src = '';
  document.removeEventListener('keydown', _lbKeyHandler);
}

function _lbKeyHandler(e) {
  if (e.key === 'Escape')      _doCloseLightbox();
  else if (e.key === 'ArrowLeft')  lightboxStep(-1);
  else if (e.key === 'ArrowRight') lightboxStep(1);
}

// ------------------------------------------------------------------ //
//  Diagram thumbnail rendering                                         //
// ------------------------------------------------------------------ //
async function _renderDiagramPreviews() {
  if (typeof mermaid === 'undefined') return;
  const nodes = [...document.querySelectorAll('.dgm-thumb-inner .mermaid')]
    .filter(n => !n.hasAttribute('data-processed') && n.textContent.trim());
  if (nodes.length === 0) return;
  try {
    await mermaid.run({ nodes });
    // Remove fixed dimensions so the SVG scales with CSS
    nodes.forEach(n => {
      const svg = n.querySelector('svg');
      if (svg) {
        svg.removeAttribute('width');
        svg.removeAttribute('height');
        svg.style.maxWidth = 'none';
      }
    });
  } catch (e) {
    console.warn('Mermaid thumbnail render failed:', e);
  }
}

// ------------------------------------------------------------------ //
//  Diagram lightbox modal                                              //
// ------------------------------------------------------------------ //
async function openDiagramModal(dgmIndex) {
  const el = elementsData.find(e => e.kind === 'diagram' && e.dgm_index === dgmIndex);
  if (!el) return;

  document.getElementById('dgm-modal-title').textContent = el.title || '다이어그램';
  const content = document.getElementById('dgm-modal-content');
  // Fresh div so Mermaid always re-renders
  content.innerHTML = `<div class="mermaid">${el.mermaid_code || ''}</div>`;
  document.getElementById('diagram-modal').classList.remove('hidden');
  document.addEventListener('keydown', _dgmKeyHandler);

  if (typeof mermaid !== 'undefined') {
    try {
      await mermaid.run({ nodes: [content.querySelector('.mermaid')] });
      const svg = content.querySelector('svg');
      if (svg) { svg.removeAttribute('width'); svg.style.maxWidth = '100%'; svg.style.height = 'auto'; }
    } catch (e) {
      content.innerHTML = `<pre class="text-xs bg-gray-50 p-4 rounded overflow-auto whitespace-pre-wrap">${escHtml(el.mermaid_code || '')}</pre>`;
    }
  }
}

function closeDiagramModal(event) {
  if (event && event.target !== document.getElementById('diagram-modal')) return;
  _doCloseDiagramModal();
}

function _doCloseDiagramModal() {
  document.getElementById('diagram-modal').classList.add('hidden');
  document.getElementById('dgm-modal-content').innerHTML = '';
  document.removeEventListener('keydown', _dgmKeyHandler);
}

function _dgmKeyHandler(e) {
  if (e.key === 'Escape') _doCloseDiagramModal();
}

// ------------------------------------------------------------------ //
//  Utils                                                               //
// ------------------------------------------------------------------ //
function escHtml(str) {
  return String(str).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function escAttr(str) {
  return String(str).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
