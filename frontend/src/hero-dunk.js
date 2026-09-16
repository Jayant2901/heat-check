/* A procedural, scroll-scrubbed dunk. It intentionally uses geometry and a
   canvas ball texture instead of an illustrated sprite, so rim depth, hand
   occlusion, net deformation and low-angle camera movement all stay physical. */

const clamp = (v, min = 0, max = 1) => Math.min(max, Math.max(min, v));
const smooth = (v) => v * v * (3 - 2 * v);
const mix = (a, b, t) => a + (b - a) * t;

function makeBallTexture() {
  const canvas = document.createElement("canvas");
  canvas.width = 1024; canvas.height = 512;
  const ctx = canvas.getContext("2d");
  const gradient = ctx.createRadialGradient(385, 170, 18, 510, 270, 560);
  gradient.addColorStop(0, "#ef9a55"); gradient.addColorStop(.46, "#c9602b"); gradient.addColorStop(1, "#743015");
  ctx.fillStyle = gradient; ctx.fillRect(0, 0, canvas.width, canvas.height);
  const grain = ctx.createImageData(canvas.width, canvas.height);
  for (let i = 0; i < 9000; i++) {
    const x = (Math.random() * canvas.width) | 0, y = (Math.random() * canvas.height) | 0;
    const offset = (y * canvas.width + x) * 4, shade = 28 + ((Math.random() * 42) | 0);
    grain.data[offset] = 38; grain.data[offset + 1] = 16; grain.data[offset + 2] = 7; grain.data[offset + 3] = shade;
  }
  ctx.putImageData(grain, 0, 0);
  ctx.strokeStyle = "#1b0e07"; ctx.lineWidth = 8; ctx.lineCap = "round";
  const seam = (fn) => { ctx.beginPath(); for (let x = -12; x <= 1036; x += 8) { const y = fn(x); x === -12 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); } ctx.stroke(); };
  ctx.beginPath(); ctx.moveTo(0, 256); ctx.lineTo(1024, 256); ctx.stroke();
  ctx.beginPath(); ctx.ellipse(512, 256, 178, 470, 0, 0, Math.PI * 2); ctx.stroke();
  ctx.beginPath(); ctx.ellipse(512, 256, 475, 177, 0, 0, Math.PI * 2); ctx.stroke();
  seam((x) => 256 + Math.sin((x / 1024) * Math.PI * 2) * 168);
  seam((x) => 256 - Math.sin((x / 1024) * Math.PI * 2) * 168);
  const texture = new THREE.CanvasTexture(canvas); texture.colorSpace = THREE.SRGBColorSpace; texture.wrapS = THREE.RepeatWrapping;
  return texture;
}

function cylinderBetween(a, b, radiusTop, radiusBottom, material, radial = 14) {
  const direction = new THREE.Vector3().subVectors(b, a); const mid = new THREE.Vector3().addVectors(a, b).multiplyScalar(.5);
  const mesh = new THREE.Mesh(new THREE.CylinderGeometry(radiusTop, radiusBottom, direction.length(), radial, 1, false), material);
  mesh.position.copy(mid); mesh.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.normalize());
  return mesh;
}

function createNet(rim, material) {
  const vertices = []; const strandCount = 12; const levels = 8;
  for (let strand = 0; strand < strandCount; strand++) for (let level = 0; level < levels; level++) vertices.push(0, 0, 0);
  for (let ring = 1; ring < levels; ring++) for (let strand = 0; strand < strandCount; strand++) {
    vertices.push(0, 0, 0, 0, 0, 0);
  }
  const geometry = new THREE.BufferGeometry(); geometry.setAttribute("position", new THREE.Float32BufferAttribute(vertices, 3));
  const lines = new THREE.LineSegments(geometry, material); rim.add(lines);
  const update = (bulge = 0) => {
    const position = geometry.attributes.position; let index = 0;
    for (let strand = 0; strand < strandCount; strand++) {
      const theta = (strand / strandCount) * Math.PI * 2;
      for (let level = 0; level < levels; level++) {
        const p = level / (levels - 1); const radius = mix(.75, .42, p) + clamp(bulge) * Math.sin(Math.PI * p) * .55;
        position.setXYZ(index++, Math.cos(theta) * radius, -p * 1.55 * (1 + .25 * clamp(bulge)), Math.sin(theta) * radius);
      }
    }
    for (let ring = 1; ring < levels; ring++) for (let strand = 0; strand < strandCount; strand++) {
      const thetaA = (strand / strandCount) * Math.PI * 2, thetaB = ((strand + 1) / strandCount) * Math.PI * 2;
      const p = ring / (levels - 1); const radius = mix(.75, .42, p) + clamp(bulge) * Math.sin(Math.PI * p) * .55; const y = -p * 1.55 * (1 + .25 * clamp(bulge));
      position.setXYZ(index++, Math.cos(thetaA) * radius, y, Math.sin(thetaA) * radius);
      position.setXYZ(index++, Math.cos(thetaB) * radius, y, Math.sin(thetaB) * radius);
    }
    position.needsUpdate = true; geometry.computeBoundingSphere();
  };
  update(0); return update;
}

function makeHand(skin) {
  const group = new THREE.Group();
  const palm = new THREE.Mesh(new THREE.SphereGeometry(.38, 22, 16), skin); palm.scale.set(1.12, .35, .9); palm.position.set(0, -.07, -.3); group.add(palm);
  const wrist = new THREE.Mesh(new THREE.SphereGeometry(.24, 18, 14), skin); wrist.position.set(0, -.34, -.55); group.add(wrist);
  const forearm = cylinderBetween(new THREE.Vector3(0, -.42, -.6), new THREE.Vector3(.06, -2.72, -1.34), .2, .32, skin, 18); group.add(forearm);
  const elbow = new THREE.Mesh(new THREE.SphereGeometry(.31, 18, 14), skin); elbow.position.set(.06, -2.72, -1.34); group.add(elbow);
  const upper = cylinderBetween(new THREE.Vector3(.06, -2.82, -1.4), new THREE.Vector3(.3, -5.45, -2.08), .28, .42, skin, 18); group.add(upper);
  const shoulder = new THREE.Mesh(new THREE.SphereGeometry(.48, 18, 14), skin); shoulder.position.set(.3, -5.45, -2.08); group.add(shoulder);
  const fingerData = [[-.46,.14,.16],[ -.16,.23,.31],[.16,.25,.36],[.45,.18,.31]];
  fingerData.forEach(([x, y, length]) => {
    const base = new THREE.Vector3(x, y, .08); const middle = base.clone().add(new THREE.Vector3(x * .12, .22, .32)); const tip = middle.clone().add(new THREE.Vector3(x * .08, -.03, .25));
    group.add(cylinderBetween(base, middle, .065, .08, skin, 12)); group.add(cylinderBetween(middle, tip, .052, .064, skin, 12));
    const joint = new THREE.Mesh(new THREE.SphereGeometry(.073, 12, 10), skin); joint.position.copy(middle); group.add(joint);
    const cap = new THREE.Mesh(new THREE.SphereGeometry(.06, 12, 10), skin); cap.scale.set(1, 1.1, 1.25); cap.position.copy(tip); group.add(cap);
  });
  const thumbA = new THREE.Vector3(-.38, -.03, -.05), thumbB = new THREE.Vector3(-.62, -.08, .22), thumbC = new THREE.Vector3(-.64, .06, .42);
  group.add(cylinderBetween(thumbA, thumbB, .07, .09, skin, 12), cylinderBetween(thumbB, thumbC, .055, .07, skin, 12));
  const thumbTip = new THREE.Mesh(new THREE.SphereGeometry(.064, 12, 10), skin); thumbTip.position.copy(thumbC); group.add(thumbTip);
  return group;
}

export function mountDunkHero(container, scrollSection) {
  if (!container || !scrollSection || !window.THREE) return;
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const heroCopy = document.querySelector("[data-hero-copy]");
  container.replaceChildren();
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2)); renderer.outputColorSpace = THREE.SRGBColorSpace; renderer.toneMapping = THREE.ACESFilmicToneMapping; renderer.toneMappingExposure = 1.12;
  container.append(renderer.domElement);
  const scene = new THREE.Scene(); const camera = new THREE.PerspectiveCamera(31, 1, .1, 100); const cameraTarget = new THREE.Vector3(0, .35, .62);
  scene.add(new THREE.HemisphereLight(0xfff7eb, 0x40231c, .9));
  const key = new THREE.DirectionalLight(0xfff3dc, 2.2); key.position.set(4, 9, 7); scene.add(key);
  const fill = new THREE.DirectionalLight(0xd6e3f4, .65); fill.position.set(-6, 3, -3); scene.add(fill);
  const rimLight = new THREE.PointLight(0xff633c, 12, 22); rimLight.position.set(-2, 4, 8); scene.add(rimLight);

  const world = new THREE.Group(); scene.add(world);
  const ink = new THREE.MeshStandardMaterial({ color: 0x201e1d, roughness: .62, metalness: .15 });
  const boardMat = new THREE.MeshStandardMaterial({ color: 0xf1eeea, roughness: .56, metalness: .05, transparent: true, opacity: .96 });
  const redMat = new THREE.MeshStandardMaterial({ color: 0xec3013, roughness: .28, metalness: .72 });
  const netMat = new THREE.LineBasicMaterial({ color: 0xe5dbd0, transparent: true, opacity: .88 });
  const backboard = new THREE.Mesh(new THREE.BoxGeometry(6, 3.5, .09), boardMat); backboard.position.set(0, 1.25, -.24); world.add(backboard);
  const boardEdges = new THREE.LineSegments(new THREE.EdgesGeometry(backboard.geometry), new THREE.LineBasicMaterial({ color: 0x201e1d })); boardEdges.position.copy(backboard.position); world.add(boardEdges);
  const square = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(-1,-.75,.06), new THREE.Vector3(1,-.75,.06), new THREE.Vector3(1,.75,.06), new THREE.Vector3(-1,.75,.06)]), new THREE.LineBasicMaterial({ color: 0xec3013, linewidth: 2 })); square.position.set(0, 1.25, -.16); world.add(square);
  const post = new THREE.Mesh(new THREE.BoxGeometry(.28, 4.5, .28), ink); post.position.set(0, -.85, -.68); world.add(post);
  const rim = new THREE.Group(); rim.position.set(0, 0, .95); rim.add(new THREE.Mesh(new THREE.TorusGeometry(.75, .055, 16, 48), redMat));
  const bracket = new THREE.Mesh(new THREE.BoxGeometry(.32, .13, 1), redMat); bracket.position.set(0, 0, -.45); rim.add(bracket); world.add(rim);
  const updateNet = createNet(rim, netMat);

  const ballTexture = makeBallTexture(); const ballMat = new THREE.MeshStandardMaterial({ map: ballTexture, bumpMap: ballTexture, bumpScale: .018, roughness: .88, metalness: 0 });
  const ball = new THREE.Mesh(new THREE.SphereGeometry(.4, 56, 40), ballMat); world.add(ball);
  const skin = new THREE.MeshStandardMaterial({ color: 0x784a34, roughness: .74, metalness: 0 }); const hand = makeHand(skin); world.add(hand);
  const ghosts = [0.2, .13, .07].map((opacity) => { const g = new THREE.Mesh(ball.geometry, new THREE.MeshBasicMaterial({ map: ballTexture, transparent: true, opacity, depthWrite: false })); world.add(g); return g; });
  const history = [];
  let progress = reducedMotion ? 1 : 0, eased = reducedMotion ? 1 : 0, destroyed = false;
  const A = new THREE.Vector3(2.95, -1.7, 2.35), B = new THREE.Vector3(2.5, 1.9, 1.5), C = new THREE.Vector3(.14, 1.35, 1.05);
  const bezier = (t) => new THREE.Vector3().copy(A).multiplyScalar((1-t)*(1-t)).addScaledVector(B, 2*(1-t)*t).addScaledVector(C, t*t);
  function resize() { const { width, height } = container.getBoundingClientRect(); if (!width || !height) return; renderer.setSize(width, height, false); camera.aspect = width / height; camera.fov = 2 * Math.atan(1.85 * 1.1 / 6.5) * 180 / Math.PI; camera.updateProjectionMatrix(); }
  const observer = new ResizeObserver(resize); observer.observe(container); resize();
  function readProgress() { const rect = scrollSection.getBoundingClientRect(); const available = Math.max(1, rect.height - innerHeight); return clamp(-rect.top / available); }
  function render() {
    if (destroyed) return; progress = reducedMotion ? 1 : readProgress(); eased += (progress - eased) * .12;
    const rise = smooth(clamp(eased / .55)); const handPos = bezier(rise); const punch = smooth(clamp((eased - .55) / .15)); const retreat = smooth(clamp((eased - .72) / .28));
    hand.position.copy(handPos).add(new THREE.Vector3(.35 * retreat, -1.05 * punch - .85 * retreat, -.12 * punch - .75 * retreat)); hand.rotation.set(mix(-.55, -1.3, rise), 0, mix(.9, .1, rise));
    const release = clamp((eased - .62) / .24); const ballPos = hand.localToWorld(new THREE.Vector3(.02, .23, .3));
    if (release > 0) ballPos.set(.14, 1.12 - release * release * 4.4, 1.03 + release * .05);
    ball.position.copy(ballPos); ball.rotation.x += .1 + release * .4; ball.rotation.z += .07 + release * .3;
    const bulge = release <= 0 ? 0 : clamp(release / .1) * (1 - clamp((release - .1) / .26)); updateNet(bulge);
    history.unshift(ballPos.clone()); if (history.length > 12) history.pop(); ghosts.forEach((ghost, i) => { const point = history[(i + 1) * 3] || ballPos; ghost.position.copy(point); ghost.visible = release > .02; });
    cameraTarget.lerp(new THREE.Vector3(ballPos.x * .22, .35 + ballPos.y * .16, .67), .08); const yaw = mix(-.26, .14, rise); camera.position.set(Math.sin(yaw) * 6.5, 1.25 + mix(-.2, -.07, rise), Math.cos(yaw) * 6.5 + .6); camera.lookAt(cameraTarget);
    renderer.domElement.style.opacity = String(reducedMotion ? 1 : clamp((eased - .02) / .1));
    if (heroCopy) {
      const copyProgress = reducedMotion ? 1 : eased;
      heroCopy.style.transform = `translate3d(0, ${-copyProgress * 44}px, 0)`;
      heroCopy.style.opacity = String(1 - copyProgress * .38);
    }
    renderer.render(scene, camera);
    if (!reducedMotion) requestAnimationFrame(render);
  }
  render();
  return () => { destroyed = true; observer.disconnect(); renderer.dispose(); ballTexture.dispose(); container.replaceChildren(); };
}
