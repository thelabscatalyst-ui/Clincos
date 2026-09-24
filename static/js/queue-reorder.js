/* Drag to reorder the waiting queue.
 *
 * Pointer Events, not the HTML5 drag-and-drop API. Native DnD does not fire
 * on touch at all, and reception runs this page on a tablet — a drag feature
 * that only works with a mouse would miss the people who need it most.
 * Pointer Events cover mouse, touch and pen through one code path.
 *
 * The reorder is optimistic: the row moves under the finger immediately and
 * the POST follows. If the server disagrees — the patient was called while
 * the drag was in flight, someone reordered from another device — the queue
 * is put back the way the server says it is.
 *
 * Keyboard works too. The handle is focusable: Space or Enter picks the row
 * up, arrows move it, Space/Enter drops it, Escape puts it back. A
 * drag-only control would lock out anyone not using a pointer, and this is
 * the screen a clinic runs its day from.
 */
(function () {
  'use strict';

  var LIST_SELECTOR = '[data-queue-reorder]';
  var ROW_SELECTOR  = '.visit-row[data-visit-id]';
  var DRAG_THRESHOLD = 4;      /* px before a press counts as a drag, so a tap
                                  on the handle can still focus it */

  function rows(list) {
    return Array.prototype.slice.call(list.querySelectorAll(ROW_SELECTOR));
  }

  function orderOf(list) {
    return rows(list).map(function (r) { return r.dataset.visitId; });
  }

  /* Token numbers deliberately do NOT change here: they are monotonic and
     printed on the patient's slip, so shuffling them would hand two people
     the same number. Position is carried by row order alone. What does need
     updating is each handle's label, which is all a screen reader has. */
  function renumber(list) {
    rows(list).forEach(function (row, i) {
      var handle = row.querySelector('.queue-handle');
      var name = (row.querySelector('.visit-name') || {}).textContent || 'Patient';
      if (handle) {
        handle.setAttribute('aria-label',
          'Reorder ' + name.trim() + ', currently position ' + (i + 1));
      }
    });
  }

  function announce(msg) {
    var live = document.getElementById('queueLive');
    if (live) live.textContent = msg;
  }

  /* ------------------------------------------------------------------ *
   *  Persisting                                                          *
   * ------------------------------------------------------------------ */

  function persist(list, row, previousOrder) {
    var position = rows(list).indexOf(row) + 1;   /* server is 1-based */
    var name = (row.querySelector('.visit-name') || {}).textContent || 'Patient';

    list.classList.add('queue-saving');

    return fetch('/visits/' + row.dataset.visitId + '/move', {
      method:      'POST',
      credentials: 'same-origin',
      headers: {
        'X-Requested-With': 'fetch',
        'Accept':           'application/json',
        'Content-Type':     'application/x-www-form-urlencoded',
      },
      body: 'new_position=' + encodeURIComponent(position),
    })
      .then(function (res) {
        var ct = res.headers.get('content-type') || '';
        if (res.redirected || ct.indexOf('application/json') === -1) {
          /* A session, PIN or plan gate bounced us and fetch followed it into
             HTML. The page is no longer trustworthy. */
          window.showToast({ message: 'Your session needs to be refreshed. Reloading…',
                             type: 'warning' });
          setTimeout(function () { window.location.href = res.url || location.href; }, 1200);
          return null;
        }
        return res.json();
      })
      .then(function (data) {
        if (!data) return;
        if (!data.ok) {
          restore(list, previousOrder);
          window.showToast({ title: 'Queue not changed', message: data.message,
                             type: 'error' });
          announce('Move cancelled. ' + data.message);
          return;
        }
        /* Reconcile against what the server actually has, so a queue that
           another device changed a second ago corrects itself instead of
           drifting. */
        if (data.order && data.order.length) {
          restore(list, data.order.map(String));
        }
        announce(name + ' moved to position ' + position + '.');
      })
      .catch(function () {
        restore(list, previousOrder);
        if (window.showRetryToast) {
          window.showRetryToast("Couldn't save the new order · check your connection",
            function () { persist(list, row, previousOrder); });
        } else {
          window.showToast({ title: 'Queue not changed',
                             message: 'Check your connection.', type: 'error' });
        }
        announce('Move failed. The queue was put back.');
      })
      .then(function () {
        list.classList.remove('queue-saving');
      });
  }

  function restore(list, order) {
    var byId = {};
    rows(list).forEach(function (r) { byId[r.dataset.visitId] = r; });
    order.forEach(function (id) {
      if (byId[id]) list.appendChild(byId[id]);
    });
    renumber(list);
  }

  /* ------------------------------------------------------------------ *
   *  Pointer dragging                                                    *
   * ------------------------------------------------------------------ */

  function attachPointer(list) {
    var dragging = null, placeholder = null, startY = 0, offsetY = 0;
    var startOrder = null, moved = false, pointerId = null;

    list.addEventListener('pointerdown', function (e) {
      var handle = e.target.closest('.queue-handle');
      if (!handle || e.button !== 0) return;
      var row = handle.closest(ROW_SELECTOR);
      if (!row) return;

      pointerId  = e.pointerId;
      dragging   = row;
      startOrder = orderOf(list);
      startY     = e.clientY;
      offsetY    = e.clientY - row.getBoundingClientRect().top;
      moved      = false;
      handle.setPointerCapture(pointerId);
      /* Don't preventDefault yet — a tap that never becomes a drag should
         still be able to focus the handle for keyboard use. */
    });

    list.addEventListener('pointermove', function (e) {
      if (!dragging || e.pointerId !== pointerId) return;

      if (!moved) {
        if (Math.abs(e.clientY - startY) < DRAG_THRESHOLD) return;
        moved = true;
        /* Now it is a drag: freeze the row's height into a placeholder so
           the list below does not jump as the row leaves the flow. */
        placeholder = document.createElement('div');
        placeholder.className = 'queue-placeholder';
        placeholder.style.height = dragging.offsetHeight + 'px';
        dragging.parentNode.insertBefore(placeholder, dragging.nextSibling);
        dragging.classList.add('queue-dragging');
        dragging.style.width = dragging.offsetWidth + 'px';
        list.classList.add('queue-is-dragging');
        document.body.classList.add('queue-drag-active');
      }
      e.preventDefault();

      var listRect = list.getBoundingClientRect();
      dragging.style.top = (e.clientY - listRect.top - offsetY) + 'px';

      /* Find the row the pointer is currently over and move the placeholder
         to the correct side of it. Midpoint, so the swap happens when the
         pointer passes the halfway line rather than the edge. */
      var others = rows(list).filter(function (r) { return r !== dragging; });
      var target = null;
      for (var i = 0; i < others.length; i++) {
        var r = others[i].getBoundingClientRect();
        if (e.clientY < r.top + r.height / 2) { target = others[i]; break; }
      }
      if (target) list.insertBefore(placeholder, target);
      else        list.appendChild(placeholder);

      autoScroll(e.clientY);
    });

    function finish(e) {
      if (!dragging || (e && e.pointerId !== pointerId)) return;
      var row = dragging;
      dragging = null;

      if (!moved) { placeholder = null; return; }   /* a tap, not a drag */

      list.insertBefore(row, placeholder);
      if (placeholder.parentNode) placeholder.parentNode.removeChild(placeholder);
      placeholder = null;

      row.classList.remove('queue-dragging');
      row.style.top = '';
      row.style.width = '';
      list.classList.remove('queue-is-dragging');
      document.body.classList.remove('queue-drag-active');
      renumber(list);

      var after = orderOf(list);
      if (after.join() === startOrder.join()) return;   /* dropped where it began */
      persist(list, row, startOrder);
    }

    list.addEventListener('pointerup', finish);
    list.addEventListener('pointercancel', finish);
  }

  /* A long queue on a phone does not fit the screen, so a drag has to be able
     to reach past the fold. */
  var scrollTimer = null;
  function autoScroll(clientY) {
    var EDGE = 80, SPEED = 12;
    clearInterval(scrollTimer);
    var dir = 0;
    if (clientY < EDGE) dir = -1;
    else if (clientY > window.innerHeight - EDGE) dir = 1;
    if (!dir) return;
    scrollTimer = setInterval(function () { window.scrollBy(0, dir * SPEED); }, 16);
  }
  ['pointerup', 'pointercancel'].forEach(function (evt) {
    document.addEventListener(evt, function () { clearInterval(scrollTimer); });
  });

  /* ------------------------------------------------------------------ *
   *  Keyboard reordering                                                 *
   * ------------------------------------------------------------------ */

  function attachKeyboard(list) {
    var held = null, heldOrder = null;

    list.addEventListener('keydown', function (e) {
      var handle = e.target.closest('.queue-handle');
      if (!handle) return;
      var row = handle.closest(ROW_SELECTOR);
      if (!row) return;
      var name = (row.querySelector('.visit-name') || {}).textContent || 'Patient';

      if (e.key === ' ' || e.key === 'Enter') {
        e.preventDefault();
        if (held === row) {
          held = null;
          row.classList.remove('queue-held');
          if (orderOf(list).join() !== heldOrder.join()) persist(list, row, heldOrder);
          else announce(name + ' left in place.');
        } else {
          held = row;
          heldOrder = orderOf(list);
          row.classList.add('queue-held');
          announce(name + ' picked up. Use the arrow keys to move, space to drop.');
        }
        return;
      }

      if (e.key === 'Escape' && held === row) {
        e.preventDefault();
        restore(list, heldOrder);
        held = null;
        row.classList.remove('queue-held');
        handle.focus();
        announce('Move cancelled. ' + name + ' is back in place.');
        return;
      }

      if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
      if (held !== row) return;   /* arrows only move a row that was picked up */
      e.preventDefault();

      var all = rows(list);
      var i = all.indexOf(row);
      if (e.key === 'ArrowUp' && i > 0) {
        list.insertBefore(row, all[i - 1]);
      } else if (e.key === 'ArrowDown' && i < all.length - 1) {
        list.insertBefore(all[i + 1], row);
      } else {
        return;
      }
      renumber(list);
      handle.focus();
      announce(name + ' now at position ' + (rows(list).indexOf(row) + 1) + '.');
    });
  }

  /* ------------------------------------------------------------------ */

  document.addEventListener('DOMContentLoaded', function () {
    Array.prototype.forEach.call(
      document.querySelectorAll(LIST_SELECTOR),
      function (list) {
        if (rows(list).length < 2) return;   /* nothing to reorder */
        attachPointer(list);
        attachKeyboard(list);
        list.classList.add('queue-reorderable');
      }
    );
  });
})();
