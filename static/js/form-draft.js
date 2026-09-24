/* Form draft autosave — survives a refresh or a failed submit for a few
 * minutes so typed-in data isn't lost.
 *
 * Uses localStorage (not real cookies) so nothing is sent to the server on
 * unrelated requests, and each entry carries its own save timestamp so it
 * expires on read without needing a server round-trip. Password fields are
 * never persisted, even in memory beyond the DOM, for obvious reasons.
 *
 * Opt out of a specific form with data-no-draft="1" on the <form> tag.
 */
(function () {
  var TTL_MS = 5 * 60 * 1000; // 5 minutes
  var PREFIX = 'medtrack:draft:';
  var SKIP_TYPES = { password: 1, file: 1, submit: 1, button: 1, reset: 1 };

  function draftKey(form) {
    var id = form.getAttribute('data-draft-id') || form.getAttribute('action') || form.id || 'form';
    return PREFIX + location.pathname + ':' + id;
  }

  function isPersistable(field) {
    if (!field.name) return false;
    var type = (field.type || '').toLowerCase();
    return !SKIP_TYPES[type];
  }

  function fields(form) {
    return Array.prototype.filter.call(form.elements, isPersistable);
  }

  function saveDraft(form) {
    var data = {};
    fields(form).forEach(function (field) {
      if (field.type === 'checkbox' || field.type === 'radio') {
        if (field.checked) data[field.name] = field.value;
      } else {
        data[field.name] = field.value;
      }
    });
    try {
      localStorage.setItem(draftKey(form), JSON.stringify({ t: Date.now(), data: data, sent: false }));
    } catch (e) { /* storage full/unavailable — draft just won't persist */ }
  }

  /* Mark a draft as having been handed to the server. See shouldRestore. */
  function markSent(form) {
    var key = draftKey(form);
    var raw, parsed;
    try { raw = localStorage.getItem(key); } catch (e) { return; }
    if (!raw) return;
    try { parsed = JSON.parse(raw); } catch (e) { return; }
    if (!parsed) return;
    parsed.sent = true;
    try { localStorage.setItem(key, JSON.stringify(parsed)); } catch (e) {}
  }

  /* A draft is only ever restored when the server did NOT accept the data.
   *
   * Restoring unconditionally made the page lie. On the settings page:
   * type into Working Hours, don't save it, save Account Details instead,
   * get redirected back here — and the abandoned working-hours edits were
   * repainted into the form, looking saved. They weren't in the database,
   * but the next Save Hours click wrote them. Every page with a POST form
   * had the same trap.
   *
   * `sent` marks a draft whose form was actually submitted. After a submit
   * there are exactly two outcomes, and PerformanceNavigationTiming tells
   * them apart:
   *
   *   POST -> 303 -> GET   server accepted it   redirectCount >= 1  -> drop
   *   POST -> 200 re-render server rejected it  redirectCount === 0 -> restore
   *
   * That second case is load-bearing: routers/auth.py re-renders
   * register.html on a validation error without echoing name/email/phone
   * back into the template, so this file is the only thing that puts them
   * there. "Just clear the draft on submit" would break that form.
   *
   * A plain refresh or a Back never sets `sent`, so those always restore.
   */
  function shouldRestore(entry) {
    if (!entry.sent) return true;
    var navs = (performance.getEntriesByType && performance.getEntriesByType('navigation')) || [];
    var nav = navs[0];
    /* No timing data (older Safari): prefer losing a draft to lying about
       one. Dropping it is recoverable; a phantom "saved" value is not. */
    if (!nav) return false;
    if (nav.redirectCount > 0) return false;
    return nav.type === 'navigate' || nav.type === 'reload';
  }

  function readDraft(form) {
    var raw;
    try {
      raw = localStorage.getItem(draftKey(form));
    } catch (e) { return null; }
    if (!raw) return null;
    var parsed;
    try { parsed = JSON.parse(raw); } catch (e) { parsed = null; }
    if (!parsed || Date.now() - parsed.t > TTL_MS || !shouldRestore(parsed)) {
      try { localStorage.removeItem(draftKey(form)); } catch (e) {}
      return null;
    }
    return parsed.data;
  }

  function restoreDraft(form) {
    var data = readDraft(form);
    if (!data) return;
    fields(form).forEach(function (field) {
      if (!(field.name in data)) return;
      if (field.type === 'checkbox' || field.type === 'radio') {
        field.checked = field.value === data[field.name];
      } else {
        field.value = data[field.name];
      }
      field.dispatchEvent(new Event('input', { bubbles: true }));
      /* Also 'change': plenty of markup wires onchange= handlers that keep
         dependent UI in sync (a toggle that greys out its row, say), and an
         input-only restore left those showing the pre-restore state. */
      field.dispatchEvent(new Event('change', { bubbles: true }));
    });
    form.dispatchEvent(new CustomEvent('formdraft:restored', { detail: data }));
  }

  function attach(form) {
    if (form.hasAttribute('data-no-draft')) return;
    if ((form.method || 'get').toLowerCase() !== 'post') return;
    restoreDraft(form);
    var timer = null;
    form.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () { saveDraft(form); }, 300);
    });
    /* Inside attach(), so data-no-draft forms are skipped here too. */
    form.addEventListener('submit', function () {
      clearTimeout(timer);
      saveDraft(form);
      markSent(form);
    });
  }

  document.addEventListener('DOMContentLoaded', function () {
    Array.prototype.forEach.call(document.querySelectorAll('form'), attach);
  });
})();
