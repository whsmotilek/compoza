/**
 * Общая обвязка для 3D-сцен прототипов KOMPOZA.
 * Берёт на себя renderer, ресайз, свет и цикл отрисовки —
 * чтобы каждая сцена описывала только свою геометрию и движение.
 */
import * as THREE from 'three';

export const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;

/** Палитры айдентики. Значения сняты пиксельно из презентации — подписям в PDF верить нельзя. */
export const BRAND = {
  coral:  0xFF9775,
  greige: 0xEDE6E2,
  wine:   0x5B2824,
  cool:   0xD1E6FD,
  rose:   0xE6DDDB,
  ink:    0x000000,
  paper:  0xFFFFFF,
  wood:   0xB08968,
  oak:    0xC9A87C,
  brass:  0xC8A45C,
};

/**
 * Создаёт сцену. Возвращает управляющий объект.
 * @param {HTMLCanvasElement} canvas
 * @param {{fov?:number, bg?:number|null, shadows?:boolean}} opts
 */
export function createStage(canvas, opts = {}) {
  const { fov = 34, bg = null, shadows = true } = opts;

  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: bg === null });
  } catch (e) {
    return null; // вызывающий код покажет статичный фолбэк
  }

  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  if (shadows) {
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  }

  const scene = new THREE.Scene();
  if (bg !== null) scene.background = new THREE.Color(bg);

  const camera = new THREE.PerspectiveCamera(fov, 1, 0.1, 200);

  function resize() {
    const r = canvas.getBoundingClientRect();
    if (!r.width || !r.height) return;
    renderer.setSize(r.width, r.height, false);
    camera.aspect = r.width / r.height;
    camera.updateProjectionMatrix();
  }
  resize();
  addEventListener('resize', resize);

  /* Пока сцена за пределами экрана, кадры не считаем: теневой проход пересчитывается
     каждый кадр и грел бы процессор всё время чтения страницы. */
  let onScreen = true;
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(([e]) => { onScreen = e.isIntersecting; }, { rootMargin: '120px' }).observe(canvas);
  }

  const tickers = [];
  const stage = {
    THREE, scene, camera, renderer, resize,
    get onScreen() { return onScreen; },
    onTick(fn) { tickers.push(fn); },
    render() {
      if (!onScreen) return;
      for (const fn of tickers) fn();
      renderer.render(scene, camera);
    },
    /* принудительный кадр — нужен для первой отрисовки и отладки */
    renderNow() {
      for (const fn of tickers) fn();
      renderer.render(scene, camera);
    },
    /** Материал по умолчанию — PBR, поэтому сцене обязательно нужен свет. */
    mat: (color, roughness = 0.85, extra = {}) =>
      new THREE.MeshStandardMaterial({ color, roughness, metalness: 0.02, ...extra }),
    box: (w, h, d) => new THREE.BoxGeometry(w, h, d),
  };

  return stage;
}

/**
 * Свет: полусферический фил + один направленный ключевой.
 * Тени вешаем только на ключевой — теневой проход рисует сцену заново каждый кадр.
 */
export function addLights(stage, { sky = 0xffffff, ground = 0xE6DDDB, fill = 1.15, key = 1.5, pos = [6, 10, 5], shadowSpan = 10 } = {}) {
  const { scene, THREE } = stage;
  scene.add(new THREE.HemisphereLight(sky, ground, fill));
  const dir = new THREE.DirectionalLight(0xffffff, key);
  dir.position.set(...pos);
  dir.castShadow = true;
  dir.shadow.mapSize.set(1024, 1024);
  const c = dir.shadow.camera;
  c.left = -shadowSpan; c.right = shadowSpan; c.top = shadowSpan; c.bottom = -shadowSpan;
  c.near = 0.5; c.far = 60;
  scene.add(dir);
  return dir;
}

/** Плавное затухание значения к цели — для «догоняющей» камеры и курсорного параллакса. */
export function damp(current, target, lambda, dt) {
  return current + (target - current) * (1 - Math.exp(-lambda * dt));
}

/**
 * Множитель дистанции камеры для узких экранов.
 * Кадры подобраны под горизонтальный монитор; на телефоне тот же радиус обрезает сцену
 * по бокам, потому что горизонтальный угол обзора зависит от соотношения сторон.
 * Отодвигаем камеру ровно настолько, чтобы сохранить прежний горизонтальный охват.
 */
export function fitScale(camera, baseAspect = 1.6, max = 2.4) {
  const a = camera.aspect || 1;
  return a >= baseAspect ? 1 : Math.min(max, baseAspect / a);
}

/** true, если экран узкий — для смены композиции, а не только дистанции. */
export const isNarrow = () => matchMedia('(max-width: 860px)').matches;

/** Нормализованное положение курсора в диапазоне -1..1. Возвращает объект, который сам обновляется. */
export function pointer(el = window) {
  const p = { x: 0, y: 0 };
  if (REDUCED) return p;
  addEventListener('pointermove', e => {
    p.x = (e.clientX / innerWidth) * 2 - 1;
    p.y = (e.clientY / innerHeight) * 2 - 1;
  }, { passive: true });
  return p;
}

/** easeOutCubic */
export const easeOut = t => 1 - Math.pow(1 - Math.max(0, Math.min(1, t)), 3);

/**
 * Раскладывает общий прогресс 0..1 на n перекрывающихся отрезков.
 * Возвращает массив локальных прогрессов — по одному на элемент.
 */
export function stagger(progress, n, overlap = 0.55) {
  const span = 1 / (n - (n - 1) * overlap);
  const step = span * (1 - overlap);
  const out = [];
  for (let i = 0; i < n; i++) {
    const start = i * step;
    out.push(Math.max(0, Math.min(1, (progress - start) / span)));
  }
  return out;
}
