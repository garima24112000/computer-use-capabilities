// Element enumeration for one frame. Evaluated by Playwright as `frame.evaluate(SCAN_JS, opts)`.
// opts: {mode: "observe" | "resolve", start: int, maxText: int, maxFrameText: int}
//  - observe: clears and reassigns sequential `data-cua-ref` attributes (e<start+1>...).
//  - resolve: leaves `data-cua-ref` untouched (so the last observation's refs stay valid).
// Every scanned element also gets a stable per-document `data-cua-tmp` handle that Python uses to
// act on the element found by a resolve. Neither attribute may ever be persisted in a TargetSpec.
(opts) => {
  const MAX_OWN = 120;
  const INTERACTIVE = 'a[href], button, input:not([type=hidden]), select, textarea, [onclick]';
  const TEXT_TAGS = new Set(['TD', 'TH', 'SPAN', 'B', 'STRONG', 'FONT', 'DIV', 'P', 'H1', 'H2',
    'H3', 'H4', 'H5', 'H6', 'LABEL', 'LI', 'CENTER', 'EM', 'I', 'U', 'SMALL', 'BIG', 'CAPTION']);
  const SKIP_TAGS = new Set(['HTML', 'HEAD', 'SCRIPT', 'STYLE', 'TITLE', 'META', 'LINK', 'NOSCRIPT',
    'FRAMESET', 'FRAME', 'NOFRAMES', 'OPTION', 'OPTGROUP', 'BODY']);

  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  // Accessible-name cleanup: collapse whitespace, drop decorative leading glyphs and trailing ':'.
  const clean = (s) => norm(s).replace(/^[»›•·]+\s*/, '').replace(/\s*:$/, '').trim();
  const ownText = (el) => norm(Array.from(el.childNodes)
    .filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent).join(' '));

  // ---- handles ---------------------------------------------------------------------------------
  if (opts.mode === 'observe') {
    document.querySelectorAll('[data-cua-ref]').forEach((e) => e.removeAttribute('data-cua-ref'));
  }
  window.__cuaTmpSeq = window.__cuaTmpSeq || 0;
  const handleOf = (el) => {
    if (!el.hasAttribute('data-cua-tmp')) el.setAttribute('data-cua-tmp', 't' + (++window.__cuaTmpSeq));
    return el.getAttribute('data-cua-tmp');
  };

  // ---- role & name -----------------------------------------------------------------------------
  const inputType = (el) => (el.tagName === 'INPUT' ? (el.getAttribute('type') || 'text').toLowerCase() : null);

  function roleOf(el) {
    const tag = el.tagName;
    if (tag === 'A' && el.hasAttribute('href')) return 'link';
    if (tag === 'BUTTON') return 'button';
    if (tag === 'INPUT') {
      const t = inputType(el);
      if (['submit', 'button', 'image', 'reset'].includes(t)) return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      return 'textbox';
    }
    if (tag === 'SELECT') return 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (el.hasAttribute('onclick')) return 'button';
    if (tag === 'TD' || tag === 'TH') return 'cell';
    return 'text';
  }

  // Text of the nearest preceding sibling cell (in the same row) that has any text.
  function precedingCellText(el) {
    const cell = el.closest('td, th');
    if (!cell) return '';
    for (let p = cell.previousElementSibling; p; p = p.previousElementSibling) {
      const t = clean(p.innerText);
      if (t) return t.slice(0, MAX_OWN);
    }
    return '';
  }

  function explicitLabel(el) {
    if (el.labels && el.labels.length) return clean(el.labels[0].innerText);
    return clean(el.getAttribute('aria-label') || el.getAttribute('title') || '');
  }

  function imageInputName(el) {
    const src = (el.getAttribute('src') || '').split(/[?#]/)[0];
    const stem = src.substring(src.lastIndexOf('/') + 1).replace(/\.[a-z0-9]+$/i, '');
    return clean(stem.replace(/^(btn|button|img|ico)[_-]/i, '').replace(/[_-]+/g, ' ').toUpperCase());
  }

  function nameOf(el, role) {
    const explicit = explicitLabel(el);
    if (explicit) return explicit;
    const t = inputType(el);
    if (['textbox', 'combobox', 'checkbox', 'radio'].includes(role)) {
      return precedingCellText(el) || clean(el.getAttribute('placeholder') || '');
    }
    if (role === 'button' && el.tagName === 'INPUT') {
      if (t === 'image') return clean(el.getAttribute('alt') || '') || imageInputName(el);
      return clean(el.getAttribute('value') || '') || (t === 'reset' ? 'Reset' : 'Submit');
    }
    if (role === 'cell' || role === 'text') return clean(ownText(el));
    const inner = clean(el.innerText);
    if (inner) return inner;
    const img = el.querySelector('img[alt]');
    return img ? clean(img.getAttribute('alt')) : '';
  }

  // ---- table context ---------------------------------------------------------------------------
  const cellsOf = (tr) => Array.from(tr.cells);
  const columnIndex = (cell) => {
    let i = 0;
    for (let c = cell.previousElementSibling; c; c = c.previousElementSibling) i += c.colSpan || 1;
    return i;
  };
  const cellAt = (tr, idx) => {
    let i = 0;
    for (const c of cellsOf(tr)) {
      if (idx >= i && idx < i + (c.colSpan || 1)) return c;
      i += c.colSpan || 1;
    }
    return null;
  };
  const isBold = (c) => {
    if ((parseInt(getComputedStyle(c).fontWeight, 10) || 400) >= 600) return true;
    const b = c.querySelector('b, strong');
    return !!b && norm(b.innerText) === norm(c.innerText);
  };
  // A column-header row: >= 2 cells and (bgcolor on the row | all <th> | all non-empty cells bold).
  function isHeaderRow(tr) {
    const cells = cellsOf(tr);
    if (cells.length < 2) return false;
    if (tr.hasAttribute('bgcolor') || cells.every((c) => c.tagName === 'TH')) return true;
    const filled = cells.filter((c) => norm(c.innerText));
    return filled.length >= 2 && filled.every(isBold);
  }
  // A section title bar: a single-cell row whose cell is <th> or has bgcolor, short, no nested table.
  function sectionRowText(tr) {
    const cells = cellsOf(tr);
    if (cells.length !== 1) return null;
    const c = cells[0];
    if (c.querySelector('table')) return null;
    if (!(c.tagName === 'TH' || c.hasAttribute('bgcolor') || tr.hasAttribute('bgcolor'))) return null;
    const t = clean(c.innerText);
    return t && t.length <= 80 ? t : null;
  }
  function sectionHeader(el) {
    let tr = el.closest('tr');
    while (tr) {
      const table = tr.closest('table');
      const rows = Array.from(table.rows);
      for (let i = rows.indexOf(tr) - 1; i >= 0; i--) {
        const h = sectionRowText(rows[i]);
        if (h) return h;
      }
      tr = table.parentElement ? table.parentElement.closest('tr') : null;
    }
    return null;
  }
  function columnHeader(cell) {
    const tr = cell.parentElement;
    if (!tr || tr.tagName !== 'TR' || isHeaderRow(tr)) return null;
    const rows = Array.from(tr.closest('table').rows);
    const idx = columnIndex(cell);
    for (let i = rows.indexOf(tr) - 1; i >= 0; i--) {
      if (isHeaderRow(rows[i])) {
        const h = cellAt(rows[i], idx);
        return h ? clean(h.innerText) || null : null;
      }
    }
    return null;
  }
  function tableContext(el) {
    const cell = el.closest('td, th');
    const tr = el.closest('tr');
    if (!cell || !tr) return [null, []];
    const rowTexts = cellsOf(tr).map((c) => clean(c.innerText).slice(0, MAX_OWN));
    const ctx = {
      row_key: rowTexts.find((t) => t) || null,
      column_header: columnHeader(cell),
      table_caption_or_header: sectionHeader(el),
    };
    return [ctx, rowTexts];
  }

  // ---- geometry & state ------------------------------------------------------------------------
  function isVisible(el) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return false;
    if (el.checkVisibility) return el.checkVisibility({ opacityProperty: true, visibilityProperty: true });
    const cs = getComputedStyle(el);
    return cs.visibility !== 'hidden' && cs.display !== 'none';
  }
  function cssPath(el) {
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
      let i = 1;
      for (let s = n.previousElementSibling; s; s = s.previousElementSibling) if (s.tagName === n.tagName) i++;
      parts.unshift(`${n.tagName.toLowerCase()}:nth-of-type(${i})`);
    }
    return ['html', ...parts].join(' > ');
  }
  function hasValue(el, role) {
    if (role === 'checkbox' || role === 'radio') return !!el.checked;
    if (el.tagName === 'SELECT') return el.selectedIndex >= 0;
    if (role === 'textbox') return (el.value || '') !== '';
    return false;
  }

  // ---- forms & focus (risk of pressing Enter) ---------------------------------------------------
  // A control "submits" a form if it is a native submit control or its onclick/javascript: href
  // calls `<form>.submit()`. Legacy pages often reference the form by name (document.frmQ.submit()),
  // so a named reference wins over DOM ancestry.
  const SUBMIT_CALL = /(?:document\.)?([A-Za-z_$][\w$]*)?\.submit\s*\(/;
  const formKey = (f) => (f ? f.getAttribute('name') || 'form#' + Array.from(document.forms).indexOf(f) : null);
  function scriptOf(el) {
    const href = el.tagName === 'A' ? el.getAttribute('href') || '' : '';
    return (el.getAttribute('onclick') || '') + ' ' + (/^javascript:/i.test(href) ? href : '');
  }
  function submitInfo(el) {
    const t = inputType(el);
    if (el.tagName === 'INPUT' && (t === 'submit' || t === 'image')) return [formKey(el.form), true];
    if (el.tagName === 'BUTTON' && (el.getAttribute('type') || 'submit').toLowerCase() === 'submit') {
      return [formKey(el.form), true];
    }
    const m = SUBMIT_CALL.exec(scriptOf(el));
    if (m) {
      const named = m[1] && document.forms[m[1]];
      return [named ? formKey(named) : formKey(el.closest('form')), true];
    }
    return [formKey(el.form || el.closest('form')), false];
  }

  // ---- scan ------------------------------------------------------------------------------------
  const out = [];
  let seq = opts.start || 0;
  let textCount = 0;
  for (const el of document.querySelectorAll('*')) {
    if (SKIP_TAGS.has(el.tagName)) continue;
    if (el.closest('select, script, style, noscript')  && el.tagName !== 'SELECT') continue;
    const interactive = el.matches(INTERACTIVE);
    if (!interactive) {
      if (!TEXT_TAGS.has(el.tagName) || textCount >= opts.maxText) continue;
      const own = ownText(el);
      if (!own || own.length > MAX_OWN) continue;
      if (el.parentElement && el.parentElement.closest(INTERACTIVE)) continue;
      if (!isVisible(el)) continue;
      textCount++;
    }
    const role = roleOf(el);
    const [ctx, rowTexts] = tableContext(el);
    const r = el.getBoundingClientRect();
    let ref = el.getAttribute('data-cua-ref');
    if (opts.mode === 'observe') {
      ref = 'e' + (++seq);
      el.setAttribute('data-cua-ref', ref);
    }
    const label = interactive ? precedingCellText(el) || explicitLabel(el) || null : null;
    const [form, submits] = interactive ? submitInfo(el) : [null, false];
    out.push({
      ref,
      handle: handleOf(el),
      kind: interactive ? 'interactive' : 'text',
      role,
      name: nameOf(el, role),
      label,
      tag: el.tagName.toLowerCase(),
      input_type: inputType(el),
      has_value: interactive ? hasValue(el, role) : false,
      options: el.tagName === 'SELECT' ? Array.from(el.options).map((o) => clean(o.text)) : null,
      enabled: !el.disabled,
      visible: isVisible(el),
      bbox: { x: r.x, y: r.y, width: r.width, height: r.height },
      table_context: ctx,
      css_path: cssPath(el),
      row_texts: rowTexts,
      form_key: form,
      submits_form: submits,
      focused: el === document.activeElement,
    });
  }
  const root = document.body || document.documentElement;
  return {
    elements: out,
    next: seq,
    title: document.title || '',
    has_focus: document.hasFocus(),
    text: norm(root ? root.innerText : '').slice(0, opts.maxFrameText),
  };
}
