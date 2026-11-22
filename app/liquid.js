/* Pointer light on the navigation glass. No continuous loop, canvas, or app state. */
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
const coarse = matchMedia('(pointer: coarse)');
let frame = 0;
let lastTarget = null;
let point = null;
function paint() {
  frame = 0;
  if (!point || reduced.matches || coarse.matches) return;
  const surface = point.target.closest('.nav button, .object-masthead');
  if (surface) {
    const rect = surface.getBoundingClientRect();
    const x = Math.max(0, Math.min(1, (point.x - rect.left) / rect.width));
    const y = Math.max(0, Math.min(1, (point.y - rect.top) / rect.height));
    surface.style.setProperty('--glass-x', `${(x * 100).toFixed(1)}%`);
    surface.style.setProperty('--glass-y', `${(y * 100).toFixed(1)}%`);
    if (lastTarget && lastTarget !== surface) reset(lastTarget);
    lastTarget = surface;
  } else if (lastTarget) { reset(lastTarget); lastTarget = null; }
  const sidebar = point.target.closest('.sidebar');
  if (sidebar) {
    const rect = sidebar.getBoundingClientRect();
    sidebar.style.setProperty('--glass-shift-x', `${((point.x - rect.left) / rect.width * 12 - 6).toFixed(1)}px`);
    sidebar.style.setProperty('--glass-shift-y', `${((point.y - rect.top) / rect.height * 18 - 9).toFixed(1)}px`);
  }
}
function reset(el) {
  el.style.removeProperty('--glass-x');
  el.style.removeProperty('--glass-y');
}
document.addEventListener('pointermove', event => {
  if (reduced.matches || coarse.matches || event.pointerType !== 'mouse') return;
  point = { x: event.clientX, y: event.clientY, target: event.target };
  if (!frame) frame = requestAnimationFrame(paint);
}, { passive: true });
document.addEventListener('pointerleave', () => {
  point = null;
  if (lastTarget) reset(lastTarget);
  lastTarget = null;
  document.querySelectorAll('.sidebar').forEach(el => {
    el.style.removeProperty('--glass-shift-x');
    el.style.removeProperty('--glass-shift-y');
  });
});
