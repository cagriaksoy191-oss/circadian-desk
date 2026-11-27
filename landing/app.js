/* ==========================================================================
   SPECTRAL V1A — Zero-Dependency Interactive Optical Simulator & Funnel Logic
   Aligned to Pure Track A v6.2 (k=2 ±17% 3-State Metrology · Zero Radio)
   ========================================================================== */

(function () {
  'use strict';

  /**
   * DEPLOYMENT CONFIG — Edit this 1 line (`formEndpoint`) before publishing live!
   * Supports Formspree ('https://formspree.io/f/YOUR_ID') OR Web3Forms
   * ('https://api.web3forms.com/submit' + web3FormsAccessKey) OR any JSON POST webhook.
   * Optional runtime override via `window.CIRCADIAN_CONFIG` is also supported.
   * Leave empty ('') in local preview mode; zero broken requests or console 404s.
   */
  const CONFIG = Object.assign(
    {
      // Day 12: Drop raw 45s bench video into landing/assets/demo_45s.mp4 and set:
      // demoVideoSrc: 'assets/demo_45s.mp4',
      demoVideoSrc: '',

      // 1-LINE FREE WAITLIST BACKEND (Web3Forms — 250 free/mo):
      formEndpoint: 'https://api.web3forms.com/submit',
      web3FormsAccessKey: '2fe12ad8-87b4-4987-86c5-7126de4a028a',

      // Crowd Supply approved Pre-Launch URL ($109–$119 Early Bird + shipping / $129–$139 MSRP)
      crowdSupplyUrl: '',

      // Paddle or Gumroad URL for the $7 instant digital-only DIY pack
      diyKitUrl: ''
    },
    typeof window !== 'undefined' && window.CIRCADIAN_CONFIG ? window.CIRCADIAN_CONFIG : {}
  );

  const K2_REL_UNCERTAINTY = 0.17; // k = 2 expanded uncertainty (~±17%)
  const DARK_NOISE_FLOOR_LX = 0.3;

  /* ------------------------------------------------------------------------
     1. 12-Channel Multi-Spectral Simulator (CIE S 026 m-EDI & k=2 3-State)
     ------------------------------------------------------------------------ */

  const CHANNELS = [
    { id: 'F1',  nm: 405, color: '#6E5AA8', melanopic: false },
    { id: 'F2',  nm: 425, color: '#4E67B8', melanopic: false },
    { id: 'FZ',  nm: 450, color: '#357CC4', melanopic: false },
    { id: 'F3',  nm: 475, color: '#2494B8', melanopic: true  },
    { id: 'F4',  nm: 515, color: '#399D7A', melanopic: true  },
    { id: 'F5',  nm: 550, color: '#79A64B', melanopic: false },
    { id: 'FY',  nm: 555, color: '#9BA644', melanopic: false },
    { id: 'FXL', nm: 600, color: '#D49A3A', melanopic: false },
    { id: 'F6',  nm: 640, color: '#D47632', melanopic: false },
    { id: 'F7',  nm: 690, color: '#BD5232', melanopic: false },
    { id: 'F8',  nm: 745, color: '#8F4538', melanopic: false },
    { id: 'NIR', nm: 855, color: '#6E6961', melanopic: false }
  ];

  // Keyframe waypoints along 07:00 -> 23:00 matching ref_desk_scene.jpg & explainer_sheet.jpg
  const CURVE_POINTS = [
    { h: 7.0,  medi: 145, cct: 4100, lux: 205, lamp: 62, lampCct: 4300, disp: 68, dispCct: 4200 },
    { h: 8.5,  medi: 220, cct: 4800, lux: 278, lamp: 74, lampCct: 5000, disp: 80, dispCct: 4900 },
    { h: 13.0, medi: 265, cct: 5200, lux: 318, lamp: 78, lampCct: 5400, disp: 85, dispCct: 5200 },
    { h: 16.5, medi: 210, cct: 4600, lux: 265, lamp: 70, lampCct: 4800, disp: 76, dispCct: 4600 },
    { h: 19.5, medi: 46,  cct: 3100, lux: 92,  lamp: 36, lampCct: 3000, disp: 44, dispCct: 3100 },
    { h: 20.5, medi: 9.2, cct: 2450, lux: 25,  lamp: 18, lampCct: 2300, disp: 24, dispCct: 2400 },
    { h: 21.5, medi: 8.0, cct: 2300, lux: 22,  lamp: 14, lampCct: 2200, disp: 22, dispCct: 2300 },
    { h: 23.0, medi: 4.0, cct: 2100, lux: 12,  lamp: 8,  lampCct: 2000, disp: 16, dispCct: 2100 }
  ];

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function interpolateCurve(hour) {
    if (hour <= CURVE_POINTS[0].h) return { ...CURVE_POINTS[0] };
    const last = CURVE_POINTS[CURVE_POINTS.length - 1];
    if (hour >= last.h) return { ...last };

    for (let i = 0; i < CURVE_POINTS.length - 1; i++) {
      const p0 = CURVE_POINTS[i];
      const p1 = CURVE_POINTS[i + 1];
      if (hour >= p0.h && hour <= p1.h) {
        const t = (hour - p0.h) / (p1.h - p0.h);
        return {
          medi: lerp(p0.medi, p1.medi, t),
          cct: lerp(p0.cct, p1.cct, t),
          lux: lerp(p0.lux, p1.lux, t),
          lamp: lerp(p0.lamp, p1.lamp, t),
          lampCct: lerp(p0.lampCct, p1.lampCct, t),
          disp: lerp(p0.disp, p1.disp, t),
          dispCct: lerp(p0.dispCct, p1.dispCct, t)
        };
      }
    }
    return { ...CURVE_POINTS[2] };
  }

  function formatHour(hourFloat) {
    const hrs = Math.floor(hourFloat);
    const mins = Math.round((hourFloat - hrs) * 60);
    return `${String(hrs).padStart(2, '0')}:${String(mins).padStart(2, '0')}`;
  }

  function evaluateThreeState(value, isEvening, isDusk, eveningCeiling, shadowActive) {
    const unc = value * K2_REL_UNCERTAINTY;
    if (shadowActive) {
      return {
        unc,
        cssClass: 'state-uncertain',
        label: 'OCCLUDED · HELD (NOT LIVE)'
      };
    }

    if (value < DARK_NOISE_FLOOR_LX) {
      return {
        unc,
        cssClass: 'state-met',
        label: 'FLOOR <0.3 lx · LOW-LIGHT'
      };
    }

    if (isDusk) {
      return {
        unc,
        cssClass: 'state-uncertain',
        label: 'TRANSITION · SMOOTH RAMP'
      };
    }

    if (isEvening) {
      if (value + unc <= eveningCeiling) {
        return {
          unc,
          cssClass: 'state-met',
          label: `MET · ≤ ${eveningCeiling} lx`
        };
      }
      if (value - unc > eveningCeiling) {
        return {
          unc,
          cssClass: 'state-unmet',
          label: `UNMET · > ${eveningCeiling} lx`
        };
      }
      return {
        unc,
        cssClass: 'state-uncertain',
        label: `UNCERTAIN · NEAR ${eveningCeiling} lx`
      };
    }

    // Daytime target: >= 250 lx m-EDI (Brown et al. 2022 / ANSI/IES RP-46-25)
    const dayTarget = 250;
    if (value - unc >= dayTarget) {
      return {
        unc,
        cssClass: 'state-met',
        label: 'MET · ≥ 250 lx'
      };
    }
    if (value + unc < dayTarget) {
      return {
        unc,
        cssClass: 'state-unmet',
        label: 'UNMET · < 250 lx'
      };
    }
    return {
      unc,
      cssClass: 'state-uncertain',
      label: 'UNCERTAIN · NEAR 250 lx'
    };
  }

  function computeSpectrum(cct, intensityScale, unlinkedCoolOn, shadowActive) {
    const coolness = Math.max(0, Math.min(1, (cct - 2000) / 3600));
    const shadowFactor = shadowActive ? 0.24 : 1.0;

    return CHANNELS.map((ch) => {
      let base;
      if (ch.nm <= 430) {
        base = lerp(4, 34, coolness);
      } else if (ch.nm <= 460) {
        base = lerp(7, 88, coolness);
      } else if (ch.nm <= 490) {
        base = lerp(9, 76, coolness);
      } else if (ch.nm <= 530) {
        base = lerp(18, 72, coolness);
      } else if (ch.nm <= 570) {
        base = lerp(38, 78, coolness);
      } else if (ch.nm <= 620) {
        base = lerp(74, 72, coolness);
      } else if (ch.nm <= 660) {
        base = lerp(84, 58, coolness);
      } else if (ch.nm <= 710) {
        base = lerp(52, 34, coolness);
      } else {
        base = lerp(24, 18, coolness);
      }

      let level = base * intensityScale;
      if (unlinkedCoolOn) {
        // Cool blue-cyan decorative light adds unremovable 440-495nm energy
        if (ch.nm === 450) level += 48;
        if (ch.nm === 475) level += 54;
        if (ch.nm === 515) level += 26;
        if (ch.nm === 425) level += 20;
      }

      level *= shadowFactor;
      return Math.max(4, Math.min(98, Math.round(level)));
    });
  }

  function initSimulator() {
    const slider = document.getElementById('sim-time-slider');
    const timeOut = document.getElementById('sim-time-output');
    const targetPill = document.getElementById('sim-target-pill');
    const phaseLabel = document.getElementById('sim-phase-label');
    const statusLed = document.getElementById('sim-status-led');
    const unlinkedToggle = document.getElementById('sim-unlinked-toggle');
    const highSensToggle = document.getElementById('sim-highsens-toggle');
    const shadowBtn = document.getElementById('sim-shadow-btn');
    const shadowStatus = document.getElementById('sim-shadow-status');
    const alertBanner = document.getElementById('sim-alert-banner');
    const alertCopy = document.getElementById('sim-alert-copy');
    const cellMedi = document.getElementById('cell-medi');

    const outMedi = document.getElementById('out-medi');
    const outMediUnc = document.getElementById('out-medi-unc');
    const outCompliance = document.getElementById('out-compliance');
    const outCct = document.getElementById('out-cct');
    const outLux = document.getElementById('out-lux');
    const outLamp = document.getElementById('out-lamp');
    const outLampCct = document.getElementById('out-lamp-cct');
    const outDisplay = document.getElementById('out-display');
    const outDisplayCct = document.getElementById('out-display-cct');
    const explanation = document.getElementById('sim-explanation');
    const spectrumEl = document.getElementById('spectrum-bars');
    const presetBtns = Array.from(document.querySelectorAll('.preset-btn'));

    if (!slider || !spectrumEl) return;

    // Build 12 channel bar DOM nodes once
    const barFills = [];
    spectrumEl.innerHTML = '';
    CHANNELS.forEach((ch) => {
      const col = document.createElement('div');
      col.className = `spec-col${ch.melanopic ? ' is-melanopic' : ''}`;
      col.title = `${ch.id} (${ch.nm} nm)${ch.melanopic ? ' — ~480–490 nm sensitivity band' : ''}`;

      const track = document.createElement('div');
      track.className = 'spec-track';

      const fill = document.createElement('div');
      fill.className = 'spec-fill';
      fill.style.backgroundColor = ch.color;
      track.appendChild(fill);

      const label = document.createElement('span');
      label.className = 'spec-nm';
      label.textContent = String(ch.nm);

      col.appendChild(track);
      col.appendChild(label);
      spectrumEl.appendChild(col);
      barFills.push(fill);
    });

    let shadowActive = false;
    let shadowTimer = null;
    let shadowCountdown = null;

    function render() {
      const hour = parseFloat(slider.value);
      const unlinkedOn = unlinkedToggle ? unlinkedToggle.checked : false;
      const highSensOn = highSensToggle ? highSensToggle.checked : false;
      const eveningCeiling = highSensOn ? 5 : 10;

      const base = interpolateCurve(hour);
      const isEvening = hour >= 20.0;
      const isDusk = hour >= 18.0 && hour < 20.0;

      timeOut.textContent = formatHour(hour);

      presetBtns.forEach((btn) => {
        const presetH = parseFloat(btn.dataset.hour);
        btn.classList.toggle('is-active', Math.abs(presetH - hour) < 0.13);
      });

      let measuredMedi = isEvening ? Number(base.medi.toFixed(1)) : Math.round(base.medi);
      let effectiveCct = Math.round(base.cct / 50) * 50;
      let photopicLux = Math.round(base.lux);
      let lampPct = Math.round(base.lamp);
      let lampCctText = `${Math.round(base.lampCct / 50) * 50}K (shade-compensated)`;
      let dispPct = Math.round(base.disp);
      let dispCctText = `DDC/CI · ${Math.round(base.dispCct / 50) * 50}K GPU LUT`;

      let alertActive = false;
      let phaseText = 'DAYTIME CLARITY · FEEDFORWARD PROFILE ACTIVE';
      let ledClass = '';
      let noteText =
        'Midday room balance: lampshade warmth is compensated via your saved 2-minute room transfer matrix; k = 2 (±17%) flags borderline readings honestly as UNCERTAIN instead of faking compliance.';

      if (isDusk) {
        phaseText = 'DUSK TRANSITION · SMOOTH HYSTERESIS RAMP';
        ledClass = 'is-evening';
        noteText =
          'Dusk ramp: linked room lamps and the Active Display Guardrail (DDC/CI + GPU LUT) step down smoothly via a 5–10 min median filter so screen-to-room contrast stays calm.';
      }

      if (isEvening) {
        if (highSensOn) {
          measuredMedi = Number(Math.min(4.1, base.medi * 0.5).toFixed(1));
          effectiveCct = Math.min(effectiveCct, 2150);
          photopicLux = Math.max(8, Math.round(base.lux * 0.52));
          lampPct = Math.max(5, Math.round(base.lamp * 0.5));
          lampCctText = '2100K (High-Sens preset)';
          dispPct = Math.max(12, Math.round(base.disp * 0.72));
          dispCctText = 'DDC/CI · 2150K GPU LUT';
          phaseText = 'HIGH-SENSITIVITY EVENING GUARD (≤ 5 m-EDI)';
          ledClass = 'is-evening';
          noteText =
            'High-Sensitivity preset active: Active Display Guardrail dims your screen to 14–16% @ 2150K (bounding white-page output to ~3–4 lx) while linked lamps stay warm below the 5 m-EDI cap.';
        } else {
          phaseText = 'EVENING GUARD · LOW-GLARE CEILING (≤ 10 m-EDI)';
          ledClass = 'is-evening';
          noteText =
            'Evening harmony: room fixtures and Active Display Guardrail stay synchronized locally over LAN and DDC/CI below 10 m-EDI (<0.3 lx dark noise floor flagged honestly).';
        }
      }

      if (unlinkedOn) {
        if (isEvening) {
          alertActive = true;
          measuredMedi = Number((18.4 + Math.max(0, (22 - hour) * 0.4)).toFixed(1));
          effectiveCct = 4350;
          photopicLux = 34;
          lampPct = 0;
          lampCctText = 'OFF — zero warm wash added';
          dispPct = Math.min(dispPct, 15);
          dispCctText = 'DDC/CI · 2200K guardrail floor';
          phaseText = 'NON-NEGATIVE GUARD · UNLINKED COOL SOURCE DETECTED';
          ledClass = 'is-alert';
          noteText =
            'Additive Light Physics: V1A refuses to crank warm lamps to mask the unlinked cool source. Linked lamps drop to 0% and Advisory Mode flags the unlinked light.';
        } else {
          const trimmedLamp = Math.max(0, lampPct - 18);
          measuredMedi = Math.round(base.medi + 45);
          effectiveCct = Math.min(6000, effectiveCct + 250);
          photopicLux = Math.round(base.lux + 54);
          lampPct = trimmedLamp;
          lampCctText = `${Math.round(base.lampCct / 50) * 50}K (daylight/ambient trimmed)`;
          noteText =
            'Ambient harvesting: strong daylight or extra ambient flux pushes eye-level m-EDI cleanly above 250 lx even after k = 2 (±17%) uncertainty, so V1A trims linked lamps downward.';
        }
      }

      targetPill.textContent = isEvening
        ? `Ceiling: ≤ ${eveningCeiling} m-EDI`
        : 'Target: ≥ 250 m-EDI';

      const threeState = evaluateThreeState(
        measuredMedi,
        isEvening,
        isDusk,
        eveningCeiling,
        shadowActive
      );

      phaseLabel.textContent = phaseText;
      statusLed.className = `status-led${ledClass ? ' ' + ledClass : ''}`;
      alertBanner.hidden = !alertActive;
      cellMedi.classList.toggle('is-warning', alertActive);

      if (alertActive) {
        alertCopy.textContent = `Measured ${measuredMedi} ± ${threeState.unc.toFixed(1)} m-EDI breaches the ${eveningCeiling} m-EDI evening ceiling. Linked lamps cut to 0% (never adds warm wash). Switch off the unlinked cool light.`;
      }

      const mediText = measuredMedi < 20 ? measuredMedi.toFixed(1) : String(Math.round(measuredMedi));
      const uncText = measuredMedi < 20 ? threeState.unc.toFixed(1) : String(Math.round(threeState.unc));

      outMedi.textContent = mediText;
      if (outMediUnc) {
        outMediUnc.textContent = `± ${uncText} lx (CIE S 026)`;
      }
      if (outCompliance) {
        outCompliance.textContent = threeState.label;
        outCompliance.className = `compliance-pill ${threeState.cssClass}`;
      }

      outCct.textContent = `${effectiveCct}K`;
      outLux.textContent = `${photopicLux} photopic lx`;
      outLamp.textContent = lampPct === 0 ? 'OFF (0%)' : `${lampPct}%`;
      outLampCct.textContent = lampCctText;
      outDisplay.textContent = `${dispPct}%`;
      outDisplayCct.textContent = dispCctText;
      explanation.textContent = shadowActive
        ? 'Transient shadow detected on raw optical trace: the 5–10 min median filter holds room lamps rock-steady (0% flicker) and explicitly marks the readout OCCLUDED — never displaying stale data as live.'
        : noteText;

      const intensityScale = Math.max(0.22, Math.min(1.0, (photopicLux + 35) / 340));
      const levels = computeSpectrum(effectiveCct, intensityScale, unlinkedOn, shadowActive);
      levels.forEach((pct, idx) => {
        barFills[idx].style.height = `${pct}%`;
      });
    }

    slider.addEventListener('input', render);
    if (unlinkedToggle) unlinkedToggle.addEventListener('change', render);
    if (highSensToggle) highSensToggle.addEventListener('change', render);

    presetBtns.forEach((btn) => {
      btn.addEventListener('click', () => {
        slider.value = btn.dataset.hour;
        render();
      });
    });

    if (shadowBtn) {
      shadowBtn.addEventListener('click', () => {
        if (shadowActive) return;
        shadowActive = true;
        shadowBtn.disabled = true;
        let remaining = 3.0;
        shadowStatus.textContent = `Raw drop blocked · Lamps held steady (${remaining.toFixed(1)}s)`;
        render();

        shadowCountdown = window.setInterval(() => {
          remaining = Math.max(0, remaining - 0.2);
          shadowStatus.textContent = `Raw drop blocked · Lamps held steady (${remaining.toFixed(1)}s)`;
        }, 200);

        shadowTimer = window.setTimeout(() => {
          window.clearInterval(shadowCountdown);
          shadowActive = false;
          shadowBtn.disabled = false;
          shadowStatus.textContent = 'Shadow rejected · 0% lamp spike';
          render();
        }, 3000);
      });
    }

    render();
  }

  /* ------------------------------------------------------------------------
     2. Reserved 45-Sec Video Slot Mount (Zero-404 Drop-in Architecture)
     ------------------------------------------------------------------------ */

  function initDemoVideoSlot() {
    const stage = document.getElementById('demo-video-stage');
    if (!stage || !CONFIG.demoVideoSrc) return;

    const video = document.createElement('video');
    video.controls = true;
    video.playsInline = true;
    video.preload = 'metadata';
    video.poster = 'assets/ref_hardware.jpg';
    video.setAttribute('aria-label', '45-second uncut desk prototype demonstration of the V1A spectral orchestrator');

    const source = document.createElement('source');
    source.src = CONFIG.demoVideoSrc;
    source.type = 'video/mp4';
    video.appendChild(source);

    stage.innerHTML = '';
    stage.appendChild(video);
  }

  /* ------------------------------------------------------------------------
     3. Qualification Pre-Launch Form & $7 DIY Kit Wiring (Paddle / Gumroad)
     ------------------------------------------------------------------------ */

  function initFunnel() {
    const form = document.getElementById('waitlist-form');
    const errorEl = document.getElementById('form-error');
    const submitBtn = document.getElementById('form-submit-btn');
    const successEl = document.getElementById('form-success');
    const successDetail = document.getElementById('form-success-detail');
    const resetBtn = document.getElementById('form-reset-btn');
    const crowdSupplyLink = document.getElementById('crowd-supply-direct-link');
    const diyBtn = document.getElementById('diy-kit-btn');
    const diyDialog = document.getElementById('diy-dialog');
    const diyJumpForm = document.getElementById('diy-jump-form');

    if (crowdSupplyLink && CONFIG.crowdSupplyUrl) {
      crowdSupplyLink.href = CONFIG.crowdSupplyUrl;
      crowdSupplyLink.hidden = false;
    }

    if (diyBtn) {
      if (CONFIG.diyKitUrl) {
        diyBtn.textContent = 'Get the $7 Digital DIY Kit \u2192';
      }
      diyBtn.addEventListener('click', () => {
        if (CONFIG.diyKitUrl) {
          window.open(CONFIG.diyKitUrl, '_blank', 'noopener,noreferrer');
        } else if (diyDialog && typeof diyDialog.showModal === 'function') {
          diyDialog.showModal();
        }
      });
    }

    if (diyJumpForm && diyDialog) {
      diyJumpForm.addEventListener('click', () => {
        diyDialog.close();
        const diyRadio = document.querySelector('input[name="intent"][value="diy_only"]');
        if (diyRadio) diyRadio.checked = true;
        const emailInput = document.getElementById('f-email');
        if (emailInput) {
          emailInput.scrollIntoView({ behavior: 'smooth', block: 'center' });
          window.setTimeout(() => emailInput.focus(), 250);
        }
      });
    }

    if (!form) return;

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      errorEl.hidden = true;

      const gotchaInput = document.getElementById('f-gotcha');
      if (gotchaInput && gotchaInput.value.trim() !== '') {
        // Silently ignore automated spam-bot submissions
        form.hidden = true;
        successEl.hidden = false;
        return;
      }

      const emailInput = document.getElementById('f-email');
      const email = emailInput.value.trim();
      if (!email || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
        errorEl.textContent = 'Please enter a valid email address so we can send the free desktop beta, 45s demo, and Crowd Supply link.';
        errorEl.hidden = false;
        emailInput.focus();
        return;
      }

      const ecosystems = Array.from(form.querySelectorAll('input[name="ecosystem"]:checked')).map(
        (el) => el.value
      );
      const intentInput = form.querySelector('input[name="intent"]:checked');

      const payload = {
        email,
        os: document.getElementById('f-os').value,
        mount: document.getElementById('f-mount').value,
        ecosystems: ecosystems.length ? ecosystems.join(', ') : 'Not specified',
        intent: intentInput ? intentInput.value : 'back_109_139',
        submitted_at: new Date().toISOString(),
        source: window.location.hostname || 'local-preview'
      };

      submitBtn.disabled = true;
      const originalBtnLabel = submitBtn.textContent;
      submitBtn.textContent = 'Saving…';

      try {
        if (CONFIG.formEndpoint) {
          const isWeb3Forms = Boolean(CONFIG.web3FormsAccessKey) || CONFIG.formEndpoint.includes('web3forms.com');
          const bodyData = isWeb3Forms
            ? {
                access_key: CONFIG.web3FormsAccessKey,
                subject: 'Circadian Desk / V1A Pre-Launch Lead',
                from_name: 'Circadian Desk Landing',
                botcheck: false,
                ...payload
              }
            : {
                _subject: 'Circadian Desk / V1A Pre-Launch Lead',
                ...payload
              };

          const res = await fetch(CONFIG.formEndpoint, {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              Accept: 'application/json'
            },
            body: JSON.stringify(bodyData)
          });

          let resJson = null;
          try {
            resJson = await res.json();
          } catch (_parseErr) {
            resJson = null;
          }

          if (!res.ok || (resJson && resJson.success === false)) {
            let serverMsg = `Submission error (${res.status})`;
            if (resJson && typeof resJson.message === 'string' && resJson.message.trim()) {
              serverMsg = resJson.message.trim();
            } else if (resJson && typeof resJson.error === 'string' && resJson.error.trim()) {
              serverMsg = resJson.error.trim();
            } else if (resJson && Array.isArray(resJson.errors) && resJson.errors[0] && resJson.errors[0].message) {
              serverMsg = resJson.errors[0].message;
            }
            throw new Error(serverMsg);
          }
        } else {
          // Safe local/staging preview mode: persist locally with zero broken network calls
          try {
            const existing = JSON.parse(window.localStorage.getItem('v1a_prelaunch_leads') || '[]');
            existing.push(payload);
            window.localStorage.setItem('v1a_prelaunch_leads', JSON.stringify(existing));
          } catch (_err) {
            // Ignore restricted storage contexts
          }
        }

        const modeNote = CONFIG.formEndpoint
          ? ''
          : ' (Local preview mode: connect Formspree or Web3Forms in app.js before public launch.)';

        successDetail.textContent = `Logged ${payload.email} (${payload.os} · ${payload.mount} · ${payload.ecosystems}). We will notify you with the free desktop app, 7-day m-EDI log, 45s bench video, and $109–$119 Early Bird (+ shipping) Crowd Supply launch.${modeNote}`;

        form.hidden = true;
        successEl.hidden = false;
      } catch (_err) {
        errorEl.textContent =
          'Could not submit right now. Please check your connection and try again.';
        errorEl.hidden = false;
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = originalBtnLabel;
      }
    });

    if (resetBtn) {
      resetBtn.addEventListener('click', () => {
        successEl.hidden = true;
        form.hidden = false;
      });
    }
  }

  /* ------------------------------------------------------------------------
     4. Accessible Dialog Modals (Explainer Lightbox & DIY Preview)
     ------------------------------------------------------------------------ */

  function initModals() {
    const zoomBtn = document.getElementById('explainer-zoom-btn');
    const lightbox = document.getElementById('lightbox-dialog');

    if (zoomBtn && lightbox && typeof lightbox.showModal === 'function') {
      zoomBtn.addEventListener('click', () => lightbox.showModal());
    }

    document.querySelectorAll('dialog.modal-dialog').forEach((dialog) => {
      dialog.querySelectorAll('[data-close-modal]').forEach((btn) => {
        btn.addEventListener('click', () => dialog.close());
      });

      dialog.addEventListener('click', (event) => {
        const rect = dialog.getBoundingClientRect();
        const inside =
          event.clientX >= rect.left &&
          event.clientX <= rect.right &&
          event.clientY >= rect.top &&
          event.clientY <= rect.bottom;
        if (!inside) dialog.close();
      });
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    initSimulator();
    initDemoVideoSlot();
    initFunnel();
    initModals();
  });
})();
