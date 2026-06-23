// HomePujan shared booking dialogs (consult + pay). Host page must define serviceData + load qrcode + Cal init.
  // --- Conversion tracking. Fires GA4/Ads events via gtag; safe no-op if gtag is absent. ---
  function hpTrack(name, params) {
    try { if (typeof gtag === 'function') gtag('event', name, params || {}); } catch (e) {}
  }
  // Fire a lead conversion when a free consultation is booked through the Cal.com embed.
  try {
    if (typeof Cal === 'function') {
      var _hpConsultBooked = function () {
        hpTrack('generate_lead', { method: 'consultation', currency: 'INR', value: 0 });
      };
      Cal('on', { action: 'bookingSuccessful', callback: _hpConsultBooked });
      if (Cal.ns && Cal.ns['15min']) {
        Cal.ns['15min']('on', { action: 'bookingSuccessful', callback: _hpConsultBooked });
      }
    }
  } catch (e) {}
  let _modalOpenCount = 0;
  function _lockBodyScroll() {
    if (_modalOpenCount > 0) return;
    const scrollY = window.scrollY;
    document.body.dataset.lockedScrollY = String(scrollY);
    document.body.style.position = 'fixed';
    document.body.style.top = `-${scrollY}px`;
    document.body.style.left = '0';
    document.body.style.right = '0';
    document.body.style.width = '100%';
    document.body.style.overflow = 'hidden';
    document.body.classList.add('modal-open');
  }
  function _unlockBodyScroll() {
    if (_modalOpenCount > 0) return;
    const scrollY = parseInt(document.body.dataset.lockedScrollY || '0', 10);
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.left = '';
    document.body.style.right = '';
    document.body.style.width = '';
    document.body.style.overflow = '';
    document.body.classList.remove('modal-open');
    window.scrollTo(0, scrollY);
  }
  function openModal(id) {
    const m = document.getElementById('modal-' + id);
    if (!m) return;
    m.style.display = 'flex';
    _lockBodyScroll();
    _modalOpenCount++;
    requestAnimationFrame(() => requestAnimationFrame(() => m.classList.add('visible')));
  }
  function closeModal(id) {
    const m = document.getElementById('modal-' + id);
    if (!m) return;
    m.classList.remove('visible');
    setTimeout(() => {
      m.style.display = 'none';
      _modalOpenCount = Math.max(0, _modalOpenCount - 1);
      _unlockBodyScroll();
    }, 320);
  }
  document.querySelectorAll('.modal-overlay').forEach(m => {
    m.addEventListener('click', e => { if (e.target === m) closeModal(m.id.replace('modal-', '')); });
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') document.querySelectorAll('.modal-overlay.visible').forEach(m => closeModal(m.id.replace('modal-', '')));
  });

  let calRenderedFor = null;

  // ── CONSULTATION MODAL ──
  function openConsultModal(service) {
    openModal('consult');
    if (typeof Cal === 'undefined') return;
    // Re-render the Cal.com embed whenever the ceremony changes, prefilling the
    // booking notes with it — so each booking records WHICH ceremony the lead
    // wants (this context was previously dropped and never reached Cal.com).
    if (calRenderedFor !== service) {
      const el = document.querySelector('#my-cal-inline-15min');
      if (el) el.innerHTML = '';
      const config = {"layout":"month_view","useSlotsViewOnSmallScreen":"true","theme":"auto"};
      if (service) config.notes = "Interested in: " + service;
      Cal.ns["15min"]("inline", {
        elementOrSelector:"#my-cal-inline-15min",
        config: config,
        calLink: "homepujan/15min",
      });
      Cal.ns["15min"]("ui", {"cssVarsPerTheme":{"light":{"cal-brand":"#4b1110"},"dark":{"cal-brand":"#d4af39"}},"hideEventTypeDetails":false,"layout":"month_view"});
      calRenderedFor = service;
    }
  }

  // ── PAYMENT WIZARD ──
  // window.__API_BASE__ may be set before this script to point at a non-same-origin backend.
  // Dev default: if the page is served from localhost (any port), assume the FastAPI backend
  // is on http://localhost:8000. In production, when frontend + backend share the same domain,

  const PAY_API_BASE = (function () {
    if (window.__API_BASE__) return window.__API_BASE__ + '/api';
    const h = (window.location && window.location.hostname) || '';
    const isLocal = h === 'localhost' || h === '127.0.0.1' || h === '0.0.0.0';
    if (isLocal) return 'http://localhost:8000/api';
    // Production backend on Railway. Change once api.homepujan.com is wired up.
    return 'https://hpujan-production.up.railway.app/api';
  })();
  const PAY_TIME_SLOTS = [
    '06:00', '07:00', '08:00', '09:00', '10:00', '11:00', '12:00', '13:00',
    '14:00', '15:00', '16:00', '17:00', '18:00', '19:00', '20:00', '21:00'
  ];
  const PAY_MAX_DAYS_AHEAD = 90;
  const PAY_WHATSAPP_NUMBER = '919667039964';

  const payState = {
    serviceId: null,
    service: null,
    selectedDate: null,
    selectedSlot: null,
    bookingId: null,
    orderId: null,
    amountPaise: null,
    serviceName: null,
    rzpKeyId: null,
    calMonth: null,
    step: 1,
    mode: null, upiConfig: null, reference: null, upiUri: null,
  };

  function payShowError(msg) {
    const el = document.getElementById('pay-error');
    if (!el) return;
    el.textContent = msg;
    el.classList.add('visible');
  }
  function payClearError() {
    const el = document.getElementById('pay-error');
    if (!el) return;
    el.textContent = '';
    el.classList.remove('visible');
  }

  function payGoTo(step) {
    payClearError();
    payState.step = step;
    document.querySelectorAll('#modal-pay .pay-step').forEach(s => s.classList.toggle('active', Number(s.dataset.step) === step));
    document.querySelectorAll('#modal-pay .pay-step-dot').forEach(d => {
      const n = Number(d.dataset.stepDot);
      d.classList.toggle('active', n === step);
      d.classList.toggle('done', n < step);
    });
    if (step === 3) payRenderSummary();
  }

  function paySameDay(a, b) {
    return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }

  function payStartOfDay(d) {
    const x = new Date(d);
    x.setHours(0, 0, 0, 0);
    return x;
  }

  function payFormatMonth(d) {
    return d.toLocaleString('en-IN', { month: 'long', year: 'numeric' });
  }
  function payFormatDate(d) {
    return d.toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
  }

  function payRenderCalendar() {
    const month = payState.calMonth;
    const grid = document.getElementById('pay-cal-grid');
    if (!grid || !month) return;
    document.getElementById('pay-cal-title').textContent = payFormatMonth(month);

    const today = payStartOfDay(new Date());
    const maxDate = new Date(today);
    maxDate.setDate(maxDate.getDate() + PAY_MAX_DAYS_AHEAD);

    // Disable prev button if showing current month
    const firstOfThisMonth = new Date(today.getFullYear(), today.getMonth(), 1);
    document.getElementById('pay-cal-prev').disabled =
      month.getFullYear() === firstOfThisMonth.getFullYear() && month.getMonth() === firstOfThisMonth.getMonth();
    // Disable next button if max range reached
    const firstOfNextShown = new Date(month.getFullYear(), month.getMonth() + 1, 1);
    document.getElementById('pay-cal-next').disabled = firstOfNextShown > maxDate;

    const days = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
    let html = days.map(d => `<div class="pay-cal-head">${d}</div>`).join('');

    const firstDay = new Date(month.getFullYear(), month.getMonth(), 1);
    const startWeekday = firstDay.getDay();
    const daysInMonth = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();

    for (let i = 0; i < startWeekday; i++) {
      html += '<button type="button" class="pay-cal-day empty" disabled></button>';
    }
    for (let day = 1; day <= daysInMonth; day++) {
      const d = new Date(month.getFullYear(), month.getMonth(), day);
      const disabled = d < today || d > maxDate;
      const selected = payState.selectedDate && paySameDay(d, payState.selectedDate);
      html += `<button type="button" class="pay-cal-day${selected ? ' selected' : ''}" ${disabled ? 'disabled' : ''} data-day="${day}">${day}</button>`;
    }
    grid.innerHTML = html;

    grid.querySelectorAll('.pay-cal-day[data-day]').forEach(btn => {
      btn.addEventListener('click', () => {
        const day = Number(btn.dataset.day);
        paySelectDate(new Date(month.getFullYear(), month.getMonth(), day));
      });
    });
  }

  function paySelectDate(d) {
    payState.selectedDate = d;
    payState.selectedSlot = null;
    payRenderCalendar();
    payRenderSlots();
    payUpdateContinue1();
  }

  function payRenderSlots() {
    const sec = document.getElementById('pay-slot-section');
    const list = document.getElementById('pay-slot-list');
    if (!payState.selectedDate) { sec.style.display = 'none'; return; }
    sec.style.display = 'block';
    list.innerHTML = PAY_TIME_SLOTS.map(t =>
      `<button type="button" class="pay-slot-pill${payState.selectedSlot === t ? ' selected' : ''}" data-slot="${t}">${t}</button>`
    ).join('');
    list.querySelectorAll('.pay-slot-pill').forEach(p => {
      p.addEventListener('click', () => {
        payState.selectedSlot = p.dataset.slot;
        payRenderSlots();
        payUpdateContinue1();
      });
    });
  }

  function payUpdateContinue1() {
    const btn = document.getElementById('pay-next-1');
    const ready = payState.selectedDate && payState.selectedSlot;
    btn.disabled = !ready;
    btn.style.opacity = ready ? '1' : '0.45';
    btn.style.cursor = ready ? 'pointer' : 'not-allowed';
  }

  function payValidateCustomer() {
    const name = document.getElementById('pay-name').value.trim();
    const email = document.getElementById('pay-email').value.trim();
    const phone = document.getElementById('pay-phone').value.trim();
    if (name.length < 2) return { ok: false, msg: 'Please enter your full name.' };
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return { ok: false, msg: 'Please enter a valid email address.' };
    const phoneDigits = phone.replace(/\D/g, '');
    if (phoneDigits.length < 10 || phoneDigits.length > 13) return { ok: false, msg: 'Please enter a valid phone number (10 digits, with or without country code).' };
    return { ok: true, customer: { name, email, phone } };
  }

  function paySlotIso() {
    const d = new Date(payState.selectedDate);
    const [h, m] = payState.selectedSlot.split(':').map(Number);
    d.setHours(h, m, 0, 0);
    return d.toISOString();
  }

  function payRenderSummary() {
    const v = payValidateCustomer();
    if (!v.ok) { payGoTo(2); payShowError(v.msg); return; }
    document.getElementById('pay-sum-service').textContent = payState.service.name;
    const dt = new Date(paySlotIso());
    document.getElementById('pay-sum-slot').textContent = `${payFormatDate(dt)} at ${payState.selectedSlot}`;
    document.getElementById('pay-sum-name').textContent = v.customer.name;
    document.getElementById('pay-sum-contact').textContent = `${v.customer.email} · ${v.customer.phone}`;
    document.getElementById('pay-sum-amount').textContent = `₹${payState.service.priceInr.toLocaleString('en-IN')}`;
  }

  async function payFetchConfig() {
    if (payState.mode) return payState;
    const r = await fetch(`${PAY_API_BASE}/payments/config`);
    if (!r.ok) throw new Error('Payment configuration unavailable.');
    const j = await r.json();
    payState.mode = j.mode || (j.key_id ? 'razorpay' : null);
    if (payState.mode === 'razorpay') {
      if (!j.key_id) throw new Error('Payment key missing.');
      payState.rzpKeyId = j.key_id;
    } else if (payState.mode === 'upi_qr') {
      if (!j.upi || !j.upi.vpa) throw new Error('UPI config missing.');
      payState.upiConfig = j.upi;
    } else {
      throw new Error('Unknown payment mode.');
    }
    const note = document.getElementById('pay-step3-note');
    if (note) {
      note.textContent = payState.mode === 'razorpay'
        ? "Dakshina does not include the cost of Samagri. You will be redirected to Razorpay's secure checkout."
        : "Dakshina does not include the cost of Samagri. After paying via UPI, send a screenshot on WhatsApp.";
    }
    return payState;
  }

  async function payDoPay() {
    payClearError();
    const v = payValidateCustomer();
    if (!v.ok) { payGoTo(2); payShowError(v.msg); return; }

    const payBtn = document.getElementById('pay-do-pay');
    payBtn.disabled = true;
    const originalLabel = payBtn.textContent;
    payBtn.textContent = 'Preparing payment…';

    try {
      await payFetchConfig();
      if (payState.mode === 'upi_qr') {
        await payDoUpiPay(v.customer, payBtn, originalLabel);
        return;
      }
      const keyId = payState.rzpKeyId;

      const orderRes = await fetch(`${PAY_API_BASE}/payments/create-order`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          service_id: payState.serviceId,
          slot_iso: paySlotIso(),
          customer: v.customer,
        }),
      });
      if (!orderRes.ok) {
        const errBody = await orderRes.json().catch(() => ({}));
        throw new Error(errBody.detail || 'Failed to create payment order.');
      }
      const order = await orderRes.json();
      payState.bookingId = order.booking_id;
      payState.orderId = order.order_id;
      payState.amountPaise = order.amount;
      payState.serviceName = order.service_name;

      if (typeof window.Razorpay === 'undefined') {
        throw new Error('Payment library failed to load. Please refresh and try again.');
      }

      const rzp = new window.Razorpay({
        key: keyId,
        amount: order.amount,
        currency: order.currency,
        name: 'HomePujan',
        description: `${order.service_name} — Direct Booking`,
        order_id: order.order_id,
        prefill: { name: v.customer.name, email: v.customer.email, contact: v.customer.phone },
        notes: { booking_id: order.booking_id, service_id: payState.serviceId, slot_iso: paySlotIso() },
        theme: { color: '#4A0E0E' },
        handler: function (resp) { payOnSuccess(resp); },
        modal: {
          ondismiss: function () {
            payShowError('Payment was cancelled. You can try again or pick a different slot.');
            payBtn.disabled = false;
            payBtn.textContent = originalLabel;
          },
        },
      });
      rzp.on('payment.failed', function (resp) {
        const desc = (resp && resp.error && resp.error.description) || 'Payment failed. Please try again.';
        payShowError(desc);
        payBtn.disabled = false;
        payBtn.textContent = originalLabel;
      });
      rzp.open();
    } catch (e) {
      payShowError(e.message || 'Something went wrong. Please try again.');
      payBtn.disabled = false;
      payBtn.textContent = originalLabel;
    }
  }

  async function payDoUpiPay(customer, payBtn, originalLabel) {
    try {
      const r = await fetch(`${PAY_API_BASE}/payments/upi-intent`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          service_id: payState.serviceId,
          slot_iso: paySlotIso(),
          customer,
        }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err.detail || 'Could not create payment request.');
      }
      const j = await r.json();
      payState.bookingId = j.booking_id;
      payState.reference = j.reference;
      payState.upiUri = j.upi_uri;
      payState.amountPaise = j.amount_paise;
      payState.serviceName = j.service_name;
      // Real booking placed (UPI): a far stronger signal than a WhatsApp click.
      hpTrack('begin_checkout', {
        currency: 'INR',
        value: (j.amount_paise || 0) / 100,
        transaction_id: j.booking_id,
        items: [{ item_id: payState.serviceId, item_name: j.service_name,
                  price: (j.amount_paise || 0) / 100, quantity: 1 }],
      });
      payRenderUpiBlock(j, customer);
      payGoTo(4);
    } catch (e) {
      payShowError(e.message || 'Could not start UPI payment.');
      payBtn.disabled = false;
      payBtn.textContent = originalLabel;
    }
  }

  function payRenderUpiBlock(j, customer) {
    document.getElementById('pay-confirm-rzp-block').style.display = 'none';
    document.getElementById('pay-upi-block').style.display = 'block';

    document.getElementById('pay-upi-ref').textContent = j.reference;
    document.getElementById('pay-upi-vpa').textContent = j.vpa;
    document.getElementById('pay-upi-vpa-2').textContent = j.vpa;
    const amountInr = j.amount_paise / 100;
    document.getElementById('pay-upi-amount').textContent = `₹${amountInr.toLocaleString('en-IN')}`;
    document.getElementById('pay-upi-open').href = j.upi_uri;

    const canvas = document.getElementById('pay-upi-qr');
    if (window.QRCode && canvas) {
      QRCode.toCanvas(canvas, j.upi_uri, { width: 220, margin: 1 }, function (err) {
        if (err) console.warn('QR render failed', err);
      });
    }

    const dt = new Date(paySlotIso());
    const waNum = j.whatsapp_number || PAY_WHATSAPP_NUMBER;
    const msg = encodeURIComponent(
      `Namaste, I have made the UPI payment for ${j.service_name}.\n` +
      `Reference: ${j.reference}\n` +
      `Amount: ₹${amountInr.toLocaleString('en-IN')}\n` +
      `Name: ${customer.name}\n` +
      `Slot: ${payFormatDate(dt)} at ${payState.selectedSlot}\n` +
      `Sharing payment screenshot here.`
    );
    document.getElementById('pay-upi-wa').href = `https://wa.me/${waNum}?text=${msg}`;
  }

  async function payOnSuccess(resp) {
    try {
      const verifyRes = await fetch(`${PAY_API_BASE}/payments/verify`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          booking_id: payState.bookingId,
          razorpay_order_id: resp.razorpay_order_id,
          razorpay_payment_id: resp.razorpay_payment_id,
          razorpay_signature: resp.razorpay_signature,
        }),
      });
      if (!verifyRes.ok) {
        const errBody = await verifyRes.json().catch(() => ({}));
        throw new Error(errBody.detail || 'Payment captured but verification failed. Please contact us.');
      }
      payRenderConfirmation();
      payGoTo(4);
    } catch (e) {
      payShowError(e.message);
      const payBtn = document.getElementById('pay-do-pay');
      if (payBtn) { payBtn.disabled = false; payBtn.textContent = 'Pay Now'; }
    }
  }

  function payRenderConfirmation() {
    // Confirmed payment (Razorpay/Cashfree synchronous success) = a true purchase.
    hpTrack('purchase', {
      transaction_id: payState.bookingId,
      currency: 'INR',
      value: (payState.amountPaise || 0) / 100,
      items: [{ item_id: payState.serviceId,
                item_name: payState.serviceName || (payState.service && payState.service.name),
                price: (payState.amountPaise || 0) / 100, quantity: 1 }],
    });
    document.getElementById('pay-confirm-rzp-block').style.display = 'block';
    document.getElementById('pay-upi-block').style.display = 'none';
    const dt = new Date(paySlotIso());
    document.getElementById('pay-confirm-id').textContent = payState.bookingId;
    document.getElementById('pay-confirm-service').textContent = payState.serviceName || payState.service.name;
    document.getElementById('pay-confirm-slot').textContent = `${payFormatDate(dt)} at ${payState.selectedSlot}`;
    document.getElementById('pay-confirm-amount').textContent = `₹${(payState.amountPaise / 100).toLocaleString('en-IN')}`;
    const msg = encodeURIComponent(`Namaste, I've completed booking for ${payState.service.name} (Ref: ${payState.bookingId}) on ${payFormatDate(dt)} at ${payState.selectedSlot}. Please confirm the muhurta and Samagri list.`);
    document.getElementById('pay-confirm-wa').href = `https://wa.me/${PAY_WHATSAPP_NUMBER}?text=${msg}`;
  }

  function openPayModal(serviceId) {
    const data = serviceData[serviceId];
    if (!data || !data.priceInr) return;
    payState.serviceId = serviceId;
    payState.service = data;
    payState.selectedDate = null;
    payState.selectedSlot = null;
    payState.bookingId = null;
    payState.orderId = null;
    payState.amountPaise = null;
    payState.serviceName = null;
    payState.reference = null;
    payState.upiUri = null;
    payState.calMonth = new Date();
    payState.calMonth.setDate(1);
    payState.step = 1;

    document.getElementById('pay-service-title').textContent = data.name;
    document.getElementById('pay-service-subtitle').textContent = data.subtitle || 'Schedule your ceremony';
    document.getElementById('pay-name').value = '';
    document.getElementById('pay-email').value = '';
    document.getElementById('pay-phone').value = '';
    payClearError();
    payGoTo(1);
    payRenderCalendar();
    payRenderSlots();
    payUpdateContinue1();
    openModal('pay');

    // Warm-up the config fetch so user doesn't wait at payment step
    payFetchConfig().catch(() => { /* swallow; surface later if user clicks pay */ });
  }

  // Wire static handlers once
  document.addEventListener('DOMContentLoaded', () => {
    const prev = document.getElementById('pay-cal-prev');
    const next = document.getElementById('pay-cal-next');
    if (prev) prev.addEventListener('click', () => {
      payState.calMonth = new Date(payState.calMonth.getFullYear(), payState.calMonth.getMonth() - 1, 1);
      payRenderCalendar();
    });
    if (next) next.addEventListener('click', () => {
      payState.calMonth = new Date(payState.calMonth.getFullYear(), payState.calMonth.getMonth() + 1, 1);
      payRenderCalendar();
    });
    const n1 = document.getElementById('pay-next-1');
    if (n1) n1.addEventListener('click', () => {
      if (!payState.selectedDate || !payState.selectedSlot) return;
      payGoTo(2);
    });
    const n2 = document.getElementById('pay-next-2');
    if (n2) n2.addEventListener('click', () => {
      const v = payValidateCustomer();
      if (!v.ok) { payShowError(v.msg); return; }
      payGoTo(3);
    });
    const doPay = document.getElementById('pay-do-pay');
    if (doPay) doPay.addEventListener('click', payDoPay);

    const copyVpa = document.getElementById('pay-upi-copy-vpa');
    if (copyVpa) copyVpa.addEventListener('click', async () => {
      const vpa = document.getElementById('pay-upi-vpa-2').textContent;
      try { await navigator.clipboard.writeText(vpa); }
      catch (e) { /* fallback below */ }
      copyVpa.textContent = 'Copied';
      copyVpa.classList.add('copied');
      setTimeout(() => { copyVpa.textContent = 'Copy'; copyVpa.classList.remove('copied'); }, 1500);
    });
  });
