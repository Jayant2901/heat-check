// Reveal-on-scroll and count-up numbers for the dashboard.
//
// Content is only ever hidden by classes this script adds, so if it fails
// to run everything stays visible. With reduced motion (or no
// IntersectionObserver) nothing is hidden and numbers show their final value.

const STAGGER_MS = 70;
const COUNT_MS = 1100;

function motionAllowed() {
  return (
    "IntersectionObserver" in window &&
    !window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

function formatCount(el, value) {
  const decimals = Number(el.dataset.decimals || 0);
  const text = value.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  return `${el.dataset.prefix || ""}${text}${el.dataset.suffix || ""}`;
}

export function createReveal(root) {
  const animate = motionAllowed();
  const frames = new Set();
  let observer = null;

  function runCount(el) {
    if (el.dataset.counted) return;
    el.dataset.counted = "true";
    const target = Number(el.dataset.count);
    if (!animate || !Number.isFinite(target)) {
      el.textContent = formatCount(el, target);
      return;
    }
    const start = performance.now();
    const tick = (now) => {
      const progress = Math.min(1, (now - start) / COUNT_MS);
      const eased = 1 - Math.pow(1 - progress, 3);
      el.textContent = formatCount(el, target * eased);
      if (progress < 1) {
        const id = requestAnimationFrame((t) => {
          frames.delete(id);
          tick(t);
        });
        frames.add(id);
      }
    };
    el.textContent = formatCount(el, 0);
    tick(start);
  }

  function reveal(el) {
    el.classList.remove("reveal-pending");
    el.classList.add("reveal-in");
    el.querySelectorAll("[data-count]").forEach(runCount);
  }

  if (animate) {
    observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          observer.unobserve(entry.target);
          reveal(entry.target);
        }
      },
      { threshold: 0.15, rootMargin: "0px 0px -8% 0px" },
    );
  }

  // Set up any [data-reveal] containers not seen before.
  function scan() {
    const fresh = [...root.querySelectorAll("[data-reveal]")].filter((el) => !el.dataset.revealInit);
    fresh.forEach((el, index) => {
      el.dataset.revealInit = "true";
      if (!animate) {
        el.querySelectorAll("[data-count]").forEach(runCount);
        return;
      }
      el.classList.add("reveal-pending");
      el.style.transitionDelay = `${STAGGER_MS * (index % 4)}ms`;
      observer.observe(el);
    });
  }

  // For numbers rendered after their container was already revealed (or
  // that sit outside any reveal container). Pending ones count on reveal.
  function count(el) {
    const container = el.closest("[data-reveal]");
    if (container && container.classList.contains("reveal-pending")) return;
    runCount(el);
  }

  function destroy() {
    observer?.disconnect();
    for (const id of frames) cancelAnimationFrame(id);
    frames.clear();
  }

  return { scan, count, destroy };
}
