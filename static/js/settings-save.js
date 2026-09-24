/* Settings page — per-section saves, dirty tracking, unsaved-changes guard.
 *
 * The settings page has nine independent sections and eleven forms. It used
 * to post each one and reload the whole page, which meant saving any section
 * threw away whatever you had half-typed in every other one, and the only
 * feedback was one generic "Changes saved" for all of them.
 *
 * Now each section posts on its own via fetch and updates in place. The rest
 * of the page — including your unsaved work in it — is untouched.
 *
 * Progressive enhancement, not a rewrite: with JavaScript off every form
 * still posts and redirects exactly as it did, and the server still renders
 * its own alert. Nothing here is load-bearing for correctness.
 *
 * DOM contract, set in templates/settings.html:
 *   data-section="schedule"              opts the form into fetch saving
 *   data-section-label="Working hours"   the human name, used verbatim in copy
 *   data-guard="1"                       adds dirty tracking + the dialog
 */
(function () {
  'use strict';

  var forms = [];

  /* ------------------------------------------------------------------ *
   *  Dirty tracking                                                      *
   * ------------------------------------------------------------------ */

  /* Serialise the whole form and compare strings. One mechanism covers every
     edge case in the Working Hours grid, which is the fiddliest thing here:

       - unchecked checkboxes are absent from FormData, so a day toggled off
         is a clean key-set diff with no checked/defaultChecked bookkeeping
       - addShift() and removeShift() add and remove inputs; their names just
         appear or vanish in the next snapshot
       - onShiftChange() rewrites `min` attributes, which are not in FormData
         and so correctly count for nothing — an attribute is not data

     Sorting makes the comparison order-independent, which matters because
     reindexShifts() renames keys after a removal. */
  function snapshot(form) {
    var parts = [];
    new FormData(form).forEach(function (v, k) { parts.push(k + '\u0000' + v); });
    parts.sort();
    return parts.join('\u0001');
  }

  /* Most cards are wrapped BY their form, so the status chip in the card
     header is inside it. The PIN card is the other way round — the form sits
     inside the card, below the header — so fall back to the card. */
  function statusEl(form) {
    var el = form.querySelector('.section-status');
    if (el) return el;
    var card = form.closest && form.closest('.settings-card');
    return card ? card.querySelector('.section-status') : null;
  }

  function setStatus(form, text, kind) {
    var el = statusEl(form);
    if (!el) return;
    el.textContent = text || '';
    el.className = 'section-status' +
      (kind ? ' section-status--' + kind : '') +
      (text ? ' is-shown' : '');
  }

  function setDirty(entry, dirty) {
    if (entry.dirty === dirty) return;
    entry.dirty = dirty;
    entry.form.classList.toggle('is-dirty', dirty);
    if (dirty) setStatus(entry.form, 'Unsaved changes', 'dirty');
    else if (statusEl(entry.form) &&
             statusEl(entry.form).classList.contains('section-status--dirty')) {
      setStatus(entry.form, '', '');
    }
  }

  function dirtyEntries() {
    return forms.filter(function (e) { return e.guard && e.dirty; });
  }

  function watch(entry) {
    var form = entry.form;
    entry.baseline = snapshot(form);

    function recheck() { setDirty(entry, snapshot(form) !== entry.baseline); }

    form.addEventListener('input', recheck);
    form.addEventListener('change', recheck);

    /* addShift()/removeShift() insert and delete nodes without firing input
       or change. childList only — deliberately not attributes, because
       onShiftChange() rewrites `min` on every edit and that is not a data
       change. Watching the subtree rather than hooking those functions
       leaves the schedule code in settings.html completely untouched. */
    if (window.MutationObserver) {
      new MutationObserver(recheck).observe(form, { childList: true, subtree: true });
    }

    entry.rebaseline = function () {
      entry.baseline = snapshot(form);
      setDirty(entry, false);
    };
  }

  /* ------------------------------------------------------------------ *
   *  Unsaved-changes dialog                                              *
   * ------------------------------------------------------------------ */

  /* Resolves to 'primary' | 'secondary' | 'cancel'. */
  function confirmUnsaved(opts) {
    return new Promise(function (resolve) {
      var backdrop  = document.getElementById('unsavedModal');
      var titleEl   = document.getElementById('unsavedTitle');
      var bodyEl    = document.getElementById('unsavedBody');
      var primary   = document.getElementById('unsavedPrimary');
      var secondary = document.getElementById('unsavedSecondary');
      if (!backdrop) { resolve('secondary'); return; }   /* never block on a missing dialog */

      titleEl.textContent   = opts.title;
      bodyEl.textContent    = opts.body;
      primary.textContent   = opts.primary;
      secondary.textContent = opts.secondary;

      var previousFocus = document.activeElement;
      backdrop.classList.add('open');
      document.body.style.overflow = 'hidden';
      primary.focus();

      function close(result) {
        backdrop.classList.remove('open');
        document.body.style.overflow = '';
        primary.removeEventListener('click', onPrimary);
        secondary.removeEventListener('click', onSecondary);
        backdrop.removeEventListener('click', onBackdrop);
        document.removeEventListener('keydown', onKey, true);
        if (previousFocus && previousFocus.focus) previousFocus.focus();
        resolve(result);
      }

      function onPrimary()   { close('primary'); }
      function onSecondary() { close('secondary'); }
      function onBackdrop(e) { if (e.target === backdrop) close('cancel'); }

      function onKey(e) {
        if (e.key === 'Escape') { e.preventDefault(); close('cancel'); return; }
        /* Two focusable elements, so a hand-rolled cycle is clearer than a
           general focus trap and has no edge cases. */
        if (e.key !== 'Tab') return;
        e.preventDefault();
        (document.activeElement === primary ? secondary : primary).focus();
      }

      primary.addEventListener('click', onPrimary);
      secondary.addEventListener('click', onSecondary);
      backdrop.addEventListener('click', onBackdrop);
      document.addEventListener('keydown', onKey, true);
    });
  }

  function labelList(entries) {
    return entries.map(function (e) { return e.label; }).join(', ');
  }

  /* ------------------------------------------------------------------ *
   *  Saving                                                              *
   * ------------------------------------------------------------------ */

  function buildBody(form, submitter) {
    var fd = new FormData(form);
    /* The submitter's name/value is part of a form submission per spec but
       NOT part of new FormData(form). Without this the PIN form's "Remove"
       button would send the hidden action=set and silently take the wrong
       branch — validating three empty fields and reporting a bad PIN.
       FormData(form, submitter) does this correctly but only landed in
       Safari 16.4, which is too new to bet a silent wrong-branch on. */
    if (submitter && submitter.name) fd.set(submitter.name, submitter.value);
    return fd;
  }

  /* The blocked-date, blocked-time and price-catalog cards are lists the
     server already knows how to render. Rather than duplicating that markup
     in JS and letting the two drift, re-fetch the page and lift out the one
     subtree that changed. Every other card — and every unsaved edit in one —
     is left alone. */
  function refreshFragment(selector) {
    return fetch(location.pathname, { credentials: 'same-origin' })
      .then(function (r) { return r.text(); })
      .then(function (html) {
        var doc  = new DOMParser().parseFromString(html, 'text/html');
        var next = doc.querySelector(selector);
        var cur  = document.querySelector(selector);
        if (next && cur) cur.replaceWith(next);
        return !!(next && cur);
      })
      .catch(function () { return false; });  /* the save landed; a stale list is not worth a scare */
  }

  var CARD_FOR_SECTION = {
    blocked_dates: 'card-blocked-dates',
    blocked_times: 'card-blocked-times',
    catalog:       'card-price-catalog'
  };

  function entryFor(form) {
    for (var i = 0; i < forms.length; i++) if (forms[i].form === form) return forms[i];
    return null;
  }

  function save(form, submitter) {
    var entry = entryFor(form);
    var label = form.dataset.sectionLabel || 'Section';
    var btn   = submitter || form.querySelector('[type="submit"]');

    if (window.setBtnLoading) window.setBtnLoading(btn, true);
    if (entry && entry.guard) setStatus(form, 'Saving…', '');

    /* getAttribute, not form.action: a named control shadows the property of
       the same name, and the PIN form has <input name="action"> — so
       form.action returned that input and the save posted to
       /doctors/[object HTMLInputElement]. */
    var url = form.getAttribute('action') || location.pathname;

    return fetch(url, {
      method:      'POST',
      body:        buildBody(form, submitter),
      credentials: 'same-origin',
      headers:     { 'X-Requested-With': 'fetch', 'Accept': 'application/json' }
    })
      .then(function (res) {
        var ct = res.headers.get('content-type') || '';
        /* An auth, PIN or plan gate bounced us. fetch follows the redirect
           silently and hands back HTML, so res.json() would throw here with
           nothing to show. The page's state is no longer trustworthy either
           way — hand control back to the browser. */
        if (res.redirected || ct.indexOf('application/json') === -1) {
          window.showToast({
            message: 'Your session needs to be refreshed. Reloading…',
            type: 'warning'
          });
          setTimeout(function () { window.location.href = res.url || location.href; }, 1200);
          return { handled: true };
        }
        return res.json().then(function (data) { return { res: res, data: data }; });
      })
      .then(function (out) {
        if (out.handled) return false;
        var data = out.data;

        /* A gate that answered in JSON rather than redirecting. */
        if (data.reason) {
          window.showToast({ message: data.message, type: 'error' });
          if (data.redirect) {
            setTimeout(function () { window.location.href = data.redirect; }, 1400);
          }
          return false;
        }

        if (!data.ok) {
          window.showToast({ title: label + ' not saved', message: data.message, type: 'error' });
          if (entry && entry.guard) setStatus(form, 'Not saved', 'error');
          return false;
        }

        /* Section-specific fixups before re-snapshotting, or the section
           stays dirty forever against a baseline it can never match. */
        if (form.dataset.section === 'pin') {
          ['current_pin', 'new_pin', 'confirm_pin'].forEach(function (n) {
            var f = form.elements[n];
            if (f) f.value = '';
          });
          var badge = document.getElementById('pinBadge');
          if (badge && typeof data.pin_enabled === 'boolean') {
            badge.textContent = data.pin_enabled ? 'Enabled' : 'Not set';
            badge.className = 'badge ' +
              (data.pin_enabled ? 'badge--scheduled' : 'badge--cancelled');
          }
          /* The Current PIN field, the Remove button and two labels are all
             rendered from doctor.pin_hash, so setting or removing a PIN
             changes which controls belong in this card. Swap the form's body
             — not the <form> itself, whose identity the dirty tracker holds
             a reference to — and rebaseline against what came back. */
          if (typeof data.pin_enabled === 'boolean' &&
              data.pin_enabled !== (form.dataset.pinEnabled === '1')) {
            form.dataset.pinEnabled = data.pin_enabled ? '1' : '0';
            refreshFragment('#card-pin .pin-form-body').then(function () {
              if (entry && entry.rebaseline) entry.rebaseline();
            });
          }
        }

        window.showToast({
          message:  data.message,
          type:     data.tone || 'success',
          title:    (data.warnings && data.warnings.length) ? data.message : '',
          duration: (data.warnings && data.warnings.length) ? 7000 : 4000
        });
        if (data.warnings && data.warnings.length) {
          /* Replace the message line with the caveat; the headline already
             said what was saved. */
          var t = document.querySelector('#toast-container .toast:last-child .toast-msg');
          if (t) t.textContent = data.warnings.join(' ');
        }

        if (entry && entry.rebaseline) {
          entry.rebaseline();
          setStatus(form, 'Saved just now', 'saved');
          setTimeout(function () {
            if (!entry.dirty) setStatus(form, '', '');
          }, 4000);
        }

        var cardId = CARD_FOR_SECTION[form.dataset.section];
        if (cardId) {
          if (form.classList.contains('blocked-add-row') || form.classList.contains('catalog-add-form')) {
            form.reset();
          }
          refreshFragment('#' + cardId + ' .section-list');
        }
        return true;
      })
      .catch(function () {
        /* Reuse the existing offline pill rather than inventing a second
           affordance for the same problem. */
        if (window.showRetryToast) {
          window.showRetryToast("Couldn't save " + label + " · check your connection",
            function () {
              if (form.requestSubmit) form.requestSubmit(submitter);
              else save(form, submitter);
            });
        } else {
          window.showToast({ title: label + ' not saved', message: 'Check your connection.', type: 'error' });
        }
        if (entry && entry.guard) setStatus(form, 'Not saved', 'error');
        return false;
      })
      .then(function (ok) {
        if (window.setBtnLoading) window.setBtnLoading(btn, false);
        return ok;
      });
  }

  /* ------------------------------------------------------------------ *
   *  Submit interception                                                 *
   * ------------------------------------------------------------------ */

  /* Bubble phase, and it yields to anything that already cancelled. A
     capture-phase handler could not: the offline guard in base.html cancels
     in capture, and two forms here cancel from an inline onsubmit confirm(). */
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.dataset || !form.dataset.section) return;   /* not ours */
    if (e.defaultPrevented) return;                       /* offline, or confirm() said no */
    e.preventDefault();

    var submitter = e.submitter;

    /* Only a section save raises the dialog. Adding a blocked date or
       unpinning a price is an instant action that reloads nothing, so it
       cannot cost you the edits sitting in another card — warning about
       them there would be noise on a button that is already one click. */
    var others = form.hasAttribute('data-guard')
      ? dirtyEntries().filter(function (en) { return en.form !== form; })
      : [];

    if (!others.length) { save(form, submitter); return; }

    var thisLabel = form.dataset.sectionLabel || 'this section';
    var many = others.length > 1;
    confirmUnsaved({
      title:     many ? others.length + ' sections not saved'
                      : others[0].label + ' not saved',
      body:      'You have unsaved changes in ' + labelList(others) + '. ' +
                 'Saving ' + thisLabel + ' will not save ' + (many ? 'them.' : 'them.'),
      primary:   many ? 'Save all' : 'Save both',
      secondary: 'Save ' + thisLabel + ' only'
    }).then(function (choice) {
      if (choice === 'cancel') return;
      if (choice === 'secondary') { save(form, submitter); return; }
      /* Save the abandoned sections first, then the one that was clicked, so
         a failure in the first is visible before the second lands. */
      var chain = Promise.resolve();
      others.forEach(function (en) {
        chain = chain.then(function () { return save(en.form); });
      });
      chain.then(function () { return save(form, submitter); });
    });
  });

  /* ------------------------------------------------------------------ *
   *  Navigation guard                                                    *
   * ------------------------------------------------------------------ */

  document.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0) return;
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;

    var a = e.target.closest && e.target.closest('a[href]');
    if (!a) return;
    var href = a.getAttribute('href');
    if (!href || href.charAt(0) === '#') return;
    if (href.indexOf('javascript:') === 0) return;
    if (a.target && a.target !== '_self') return;
    if (a.hasAttribute('download')) return;

    var dirty = dirtyEntries();
    if (!dirty.length) return;

    e.preventDefault();
    var many = dirty.length > 1;
    confirmUnsaved({
      title:     many ? dirty.length + ' sections not saved'
                      : dirty[0].label + ' not saved',
      body:      'Leaving this page will discard your unsaved changes to ' +
                 labelList(dirty) + '.',
      /* "Save and stay" rather than saving then navigating: an async save
         can fail, and navigating away anyway is the exact failure this
         whole change exists to remove. */
      primary:   'Save and stay',
      secondary: 'Leave without saving'
    }).then(function (choice) {
      if (choice === 'cancel') return;
      if (choice === 'secondary') {
        dirty.forEach(function (en) { setDirty(en, false); });   /* don't re-prompt on unload */
        window.location.href = a.href;
        return;
      }
      var chain = Promise.resolve();
      dirty.forEach(function (en) { chain = chain.then(function () { return save(en.form); }); });
    });
  });

  /* Back, forward, tab close and a typed URL cannot be given a custom
     dialog — beforeunload with the browser's own wording is all there is. */
  window.addEventListener('beforeunload', function (e) {
    if (!dirtyEntries().length) return;
    e.preventDefault();
    e.returnValue = '';
  });

  /* ------------------------------------------------------------------ *
   *  Boot                                                                *
   * ------------------------------------------------------------------ */

  document.addEventListener('DOMContentLoaded', function () {
    Array.prototype.forEach.call(
      document.querySelectorAll('form[data-section]'),
      function (form) {
        var entry = {
          form:  form,
          label: form.dataset.sectionLabel || 'Section',
          guard: form.hasAttribute('data-guard'),
          dirty: false
        };
        forms.push(entry);
        if (entry.guard) watch(entry);
      }
    );
  });
})();
