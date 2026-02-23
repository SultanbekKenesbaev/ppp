function openModal(title, html){
  const modal = document.getElementById('modal');
  document.getElementById('modalTitle').innerText = title || 'Детали';
  document.getElementById('modalBody').innerHTML = html || '';
  modal.classList.remove('hidden');
}
function closeModal(){
  document.getElementById('modal').classList.add('hidden');
}

window.addEventListener('click', (e)=>{
  const modal = document.getElementById('modal');
  if (!modal || modal.classList.contains('hidden')) return;
  if (e.target === modal) closeModal();
});
function showPicked(input, listId){
  const box = document.getElementById(listId);
  if (!box) return;

  const files = Array.from(input.files || []);
  if (files.length === 0){
    box.innerHTML = '';
    return;
  }

  box.innerHTML = files.map(f => {
    const ext = (f.name.includes('.') ? f.name.split('.').pop() : 'FILE').toUpperCase();
    const kb = Math.round((f.size/1024) * 10) / 10;
    return `
      <div class="picked-item">
        <div class="picked-ext">${ext}</div>
        <div class="picked-name">${escapeHtml(f.name)}</div>
        <div class="picked-size">${kb} KB</div>
      </div>
    `;
  }).join('');
}

function escapeHtml(str){
  return (str || '').replace(/[&<>"']/g, (m) => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[m]));
}

// ===== Multi-select like Telegram (append files, not replace) =====
function initTgFilePicker(inputId, listId, onChange){
  const input = document.getElementById(inputId);
  const list  = document.getElementById(listId);
  if (!input || !list) return;
  let pickedFiles = [];

  // при выборе файлов добавляем к массиву, а не заменяем
  input.addEventListener('change', () => {
    const picked = Array.from(input.files || []);
    if (picked.length === 0) return;

    // добавляем новые
    pickedFiles = pickedFiles.concat(picked);

    // синхронизируем обратно в input.files
    syncInputFiles(input);

    // рисуем список
    renderPicked(list);

    // важно: сбросить value, чтобы можно было выбрать тот же файл снова
    input.value = '';
  });

  // удаление файла кликом по "×"
  list.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-remove]');
    if (!btn) return;

    const idx = parseInt(btn.getAttribute('data-remove'), 10);
    if (Number.isNaN(idx)) return;

    pickedFiles.splice(idx, 1);
    syncInputFiles(input);
    renderPicked(list);
  });

  function syncInputFiles(inputEl){
    const dt = new DataTransfer();
    pickedFiles.forEach(f => dt.items.add(f));
    inputEl.files = dt.files;
    if (typeof onChange === 'function') onChange(pickedFiles.length, pickedFiles);
  }

  function renderPicked(box){
    if (pickedFiles.length === 0){
      box.innerHTML = '';
      return;
    }

    box.innerHTML = pickedFiles.map((f, i) => {
      const ext = (f.name.includes('.') ? f.name.split('.').pop() : 'FILE').toUpperCase();
      const kb = Math.round((f.size/1024) * 10) / 10;
      return `
        <div class="picked-item">
          <div class="picked-ext">${escapeHtml(ext)}</div>
          <div class="picked-name">${escapeHtml(f.name)}</div>
          <div class="picked-size">${kb} KB</div>
          <button type="button" class="picked-x" data-remove="${i}" aria-label="Удалить">×</button>
        </div>
      `;
    }).join('');
  }

  if (typeof onChange === 'function') onChange(0, pickedFiles);
}
