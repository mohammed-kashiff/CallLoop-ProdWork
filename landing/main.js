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
  };
  if (motionQuery.addEventListener) {
    motionQuery.addEventListener('change', onMotionChange);
  }
})();
