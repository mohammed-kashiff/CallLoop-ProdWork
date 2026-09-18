(function () {
  'use strict';

  var prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---------- Scroll-triggered reveal (PRD §4.1) ----------
  // One-shot: once a .reveal element crosses ~20% into the viewport it gets
  // .is-visible and is never touched again — a marketing page shouldn't
  // feel jumpy by re-triggering on scroll-up.
  var revealTargets = document.querySelectorAll('.reveal');
  if ('IntersectionObserver' in window && revealTargets.length) {
    var revealObserver = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add('is-visible');
            revealObserver.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.2 },
    );
    revealTargets.forEach(function (el, i) {
      el.style.animationDelay = (i % 3) * 90 + 'ms';
      revealObserver.observe(el);
    });
  } else {
    revealTargets.forEach(function (el) {
      el.classList.add('is-visible');
    });
  }

  // ---------- Hero evidence-card carousel ----------
  // A handful of real-shaped example findings, rotating on a timer — same
  // pause-on-hover / click-to-jump-and-reset interaction language as the
  // feature-switcher tabs below, just on a shorter cycle since the card
  // is small and meant to be skimmed at a glance.
  var EVIDENCE_SLIDES = [
    {
      title: 'Resolution Correctness — verified',
      quote: '“I’ve processed yours as a one-time exception.”',
      chip: 'PASS',
    },
    {
      title: 'Compliance Disclosure — verified',
      quote: '“This call may be recorded for quality and training purposes.”',
      chip: 'PASS',
    },
    {
      title: 'De-escalation — verified',
      quote: '“Let’s slow down and go through this together, step by step.”',
      chip: 'PASS',
    },
    {
      title: 'First Contact Resolution — verified',
      quote: '“I’ve fixed it directly — no need to follow up again.”',
      chip: 'PASS',
    },
    {
      title: 'Tone & Professionalism — verified',
      quote: '“I hear you, and I want to get this right for you.”',
      chip: 'PASS',
    },
  ];
  var EVIDENCE_MS = 4500;
  var evidenceCard = document.getElementById('evidence-card');
  var evidenceDotsWrap = document.querySelector('.evidence-dots');
  var evidenceIndex = 0;
  var evidenceTimer = null;

  function renderEvidence(index) {
    var slide = EVIDENCE_SLIDES[index];
    evidenceCard.querySelector('.evidence-title').textContent = slide.title;
    evidenceCard.querySelector('.evidence-quote').textContent = slide.quote;
    evidenceCard.querySelector('.chip').textContent = slide.chip;
    var dots = evidenceDotsWrap.querySelectorAll('.evidence-dot');
    dots.forEach(function (dot, i) {
      dot.classList.toggle('is-active', i === index);
      dot.setAttribute('aria-selected', i === index ? 'true' : 'false');
    });
  }

  function setEvidence(index, opts) {
    opts = opts || {};
    evidenceIndex = ((index % EVIDENCE_SLIDES.length) + EVIDENCE_SLIDES.length) % EVIDENCE_SLIDES.length;
    if (prefersReducedMotion || opts.skipFade) {
      renderEvidence(evidenceIndex);
    } else {
      evidenceCard.classList.add('is-swapping');
      setTimeout(function () {
        renderEvidence(evidenceIndex);
        evidenceCard.classList.remove('is-swapping');
      }, 300);
    }
    if (!opts.skipTimerReset) {
      resetEvidenceTimer();
    }
  }

  function resetEvidenceTimer() {
    if (evidenceTimer) clearInterval(evidenceTimer);
    if (prefersReducedMotion) return;
    evidenceTimer = setInterval(function () {
      setEvidence(evidenceIndex + 1, { skipTimerReset: true });
    }, EVIDENCE_MS);
  }

  if (evidenceCard && evidenceDotsWrap) {
    EVIDENCE_SLIDES.forEach(function (_, i) {
      var dot = document.createElement('button');
      dot.type = 'button';
      dot.className = 'evidence-dot';
      dot.setAttribute('role', 'tab');
      dot.setAttribute('aria-label', 'Show example ' + (i + 1) + ' of ' + EVIDENCE_SLIDES.length);
      dot.addEventListener('click', function () {
        setEvidence(i);
      });
      evidenceDotsWrap.appendChild(dot);
    });

    renderEvidence(0);
    evidenceDotsWrap.querySelector('.evidence-dot').classList.add('is-active');

    evidenceCard.addEventListener('mouseenter', function () {
      if (evidenceTimer) clearInterval(evidenceTimer);
    });
    evidenceCard.addEventListener('mouseleave', resetEvidenceTimer);

    resetEvidenceTimer();
  }

  // ---------- Auto-advancing tab switcher (PRD §4.2) ----------
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tab'));
  var panels = Array.prototype.slice.call(document.querySelectorAll('.tab-panel'));
  var ADVANCE_MS = 6000;
  var advanceTimer = null;
  var activeIndex = 0;

  function setActive(index, opts) {
    opts = opts || {};
    activeIndex = ((index % tabs.length) + tabs.length) % tabs.length;

    tabs.forEach(function (tab, i) {
      var isActive = i === activeIndex;
      tab.classList.toggle('is-active', isActive);
      tab.setAttribute('aria-selected', isActive ? 'true' : 'false');
      var progress = tab.querySelector('.tab-progress');
      progress.classList.remove('is-running');
      // Force reflow so re-adding .is-running restarts the CSS animation
      // even when the same tab is re-selected (e.g. clicking the active tab).
      // eslint-disable-next-line no-unused-expressions
      progress.offsetWidth;
      if (isActive && !prefersReducedMotion) {
        progress.classList.add('is-running');
      }
    });

    panels.forEach(function (panel, i) {
      var isActive = i === activeIndex;
      panel.classList.toggle('is-active', isActive);
      panel.hidden = !isActive;
    });

    if (!opts.skipTimerReset) {
      resetTimer();
    }
  }

  function resetTimer() {
    if (advanceTimer) clearTimeout(advanceTimer);
    if (prefersReducedMotion) return;
    advanceTimer = setTimeout(function () {
      setActive(activeIndex + 1);
    }, ADVANCE_MS);
  }

  function pauseTimer() {
    if (advanceTimer) clearTimeout(advanceTimer);
    tabs[activeIndex].querySelector('.tab-progress').style.animationPlayState = 'paused';
  }

  function resumeTimer() {
    resetTimer();
  }

  if (tabs.length && panels.length) {
    tabs.forEach(function (tab, i) {
      tab.addEventListener('click', function () {
        setActive(i);
      });
    });

    var switcherSection = document.querySelector('.switcher');
    if (switcherSection) {
      switcherSection.addEventListener('mouseenter', pauseTimer);
      switcherSection.addEventListener('mouseleave', resumeTimer);
    }

    setActive(0, { skipTimerReset: true });
    resetTimer();
  }

  // ---------- Respect a live OS toggle of prefers-reduced-motion ----------
  var motionQuery = window.matchMedia('(prefers-reduced-motion: reduce)');
  var onMotionChange = function (e) {
    prefersReducedMotion = e.matches;
    if (prefersReducedMotion && advanceTimer) {
      clearTimeout(advanceTimer);
      tabs.forEach(function (tab) {
        tab.querySelector('.tab-progress').classList.remove('is-running');
      });
    } else if (!prefersReducedMotion) {
      setActive(activeIndex);
    }
    if (prefersReducedMotion && evidenceTimer) {
      clearInterval(evidenceTimer);
    } else if (!prefersReducedMotion && evidenceCard) {
      resetEvidenceTimer();
    }
  };
  if (motionQuery.addEventListener) {
    motionQuery.addEventListener('change', onMotionChange);
  }
})();
