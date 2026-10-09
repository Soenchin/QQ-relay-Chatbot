// Small, dependency-free controls shared by the relay views.
const UI_ICONS = {
    dashboard: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    settings: '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="3"/><circle cx="15" cy="17" r="3"/>',
    pipe: '<rect x="3" y="3" width="6" height="6" rx="2"/><rect x="15" y="15" width="6" height="6" rx="2"/><path d="M6 9v6a3 3 0 0 0 3 3h6M15 6h6m-3-3v6"/>',
    book: '<path d="M4 4h6l2 2 2-2h6v15h-6l-2 2-2-2H4zM12 6v15"/>',
    plugin: '<rect x="5" y="7" width="14" height="13" rx="3"/><path d="M9 3v4m6-4v4M9 12h6m-3 0v4"/>',
    send: '<path d="m21 3-7 18-4-7-7-4 18-7ZM10 14 21 3"/>',
    reload: '<path d="M20 7v5h-5M4 17v-5h5M6 7a7 7 0 0 1 12-1l2 6M4 12l2 6a7 7 0 0 0 12-1"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    save: '<path d="M5 3h12l4 4v14H3V3h2Zm2 0v7h10V3M7 21v-7h10v7"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
};
function uiIcon(name) {
    return `<svg class="ui-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${UI_ICONS[name] || UI_ICONS.dashboard}</svg>`;
}

class RollingNumber extends HTMLElement {
    static observedAttributes = ['value'];
    connectedCallback() { this.update(); }
    attributeChangedCallback() { if (this.isConnected) this.update(); }
    update() {
        const next = Math.max(0, Math.trunc(Number(this.getAttribute('value')) || 0));
        if (this.previous === next) return;
        const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
        const old = this.previous;
        const chars = String(next);
        this.setAttribute('role', 'img');
        this.setAttribute('aria-label', String(next));
        // Columns are keyed by place value, so 9 → 10 preserves the units wheel.
        const existing = new Map([...this.children].map(el => [el.dataset.place, el]));
        [...chars].forEach((char, index) => {
            const place = String(chars.length - index - 1);
            let cell = existing.get(place);
            if (!cell) {
                cell = document.createElement('span');
                cell.className = 'number-cell';
                cell.dataset.place = place;
                cell.setAttribute('aria-hidden', 'true');
                // An in-flow glyph gives the clipped wheel a real text baseline.
                cell.innerHTML = '<span class="number-sizer">0</span><span class="number-wheel">' + Array.from({length: 30}, (_, i) => `<span>${i % 10}</span>`).join('') + '</span>';
            }
            const wheel = cell.querySelector('.number-wheel');
            const digit = Number(char);
            const before = Number(cell.dataset.digit || 0);
            const resting = `translateY(${-((10 + digit) * 100 / 30)}%)`;
            if (old !== undefined && before !== digit && !reduced) {
                // Capture the visual position before cancelling: rapid updates never queue turns.
                const from = getComputedStyle(wheel).transform;
                wheel.getAnimations().forEach(a => a.cancel());
                const target = next > old && digit < before ? 20 + digit : next < old && digit > before ? digit : 10 + digit;
                wheel.style.transform = resting;
                wheel.animate([{ transform: from === 'none' ? `translateY(${-((10 + before) * 100 / 30)}%)` : from },
                    { transform: `translateY(${-(target * 100 / 30)}%)` }],
                    { duration: 460, easing: 'cubic-bezier(.22,1,.36,1)' });
            } else {
                wheel.getAnimations().forEach(a => a.cancel());
                wheel.style.transform = resting;
            }
            cell.dataset.digit = char;
            this.append(cell);
            existing.delete(place);
        });
        existing.forEach(cell => cell.remove());
        this.previous = next;
    }
}
customElements.define('rolling-number', RollingNumber);
function rollingNumber(value, key) {
    return `<rolling-number value="${Number(value) || 0}" data-counter="${key}"></rolling-number>`;
}

function mountDeleteButton(host, filename, onDelete, onDone) {
    host.className = 'delete-control';
    host.innerHTML = `<div class="delete-tile" data-state="idle">
        <button type="button" class="delete-trigger" aria-label="删除文件" aria-expanded="false" title="删除文件">
            <svg class="bin-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <path class="bin-wall" d="M5 6v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V6"/>
                <g class="bin-lid"><path d="M3 6h18M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></g>
            </svg><span class="delete-done">${uiIcon('check')}</span>
        </button>
        <div class="delete-options" hidden>
            <button type="button" class="delete-confirm" aria-label="确认删除文件" title="确认删除">${uiIcon('check')}</button>
            <button type="button" class="delete-cancel" aria-label="取消删除" title="取消">${uiIcon('close')}</button>
        </div></div><span class="delete-feedback" role="status" aria-live="polite"></span>`;
    const tile = host.querySelector('.delete-tile');
    const trigger = host.querySelector('.delete-trigger');
    const options = host.querySelector('.delete-options');
    const feedback = host.querySelector('.delete-feedback');
    let busy = false;
    let open = false;
    function setOpen(value) {
        open = value;
        tile.dataset.state = value ? 'open' : 'idle';
        trigger.setAttribute('aria-expanded', String(value));
        options.hidden = !value;
        feedback.textContent = value ? `删除「${filename}」？无法撤销。` : '';
    }
    trigger.onclick = () => { if (!busy) setOpen(!open); };
    host.querySelector('.delete-cancel').onclick = () => { setOpen(false); trigger.focus(); };
    host.onkeydown = event => {
        if (event.key === 'Escape' && open && !busy) { setOpen(false); trigger.focus(); }
    };
    host.querySelector('.delete-confirm').onclick = async () => {
        if (busy) return;
        busy = true;
        tile.dataset.state = 'pending';
        host.setAttribute('aria-busy', 'true');
        host.querySelectorAll('button').forEach(button => { button.disabled = true; });
        feedback.textContent = '正在删除…';
        try {
            await onDelete();
        } catch (error) {
            if (!host.isConnected) return;
            busy = false;
            host.removeAttribute('aria-busy');
            host.querySelectorAll('button').forEach(button => { button.disabled = false; });
            setOpen(true);
            feedback.textContent = `删除失败：${error.message}。可以重试或取消。`;
            host.querySelector('.delete-confirm').focus();
            return;
        }
        if (!host.isConnected) return;
        host.removeAttribute('aria-busy');
        tile.dataset.state = 'done';
        trigger.setAttribute('aria-expanded', 'false');
        options.hidden = true;
        feedback.textContent = '文件已删除';
        setTimeout(() => { if (host.isConnected) onDone(); }, 900);
    };
}
