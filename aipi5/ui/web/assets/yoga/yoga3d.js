/* Production mount for the runtime 3D Yoga Coach.
 *
 * The standalone v3 checker and the game use the same Coach, rigdata and VRM.
 * The game only supplies the current pose ids and transition fraction; this
 * module owns WebGL and never reaches into the pose/camera pipeline.
 */
import * as THREE from "three";
import { GLTFLoader } from "./v3/lib/GLTFLoader.js";
import { Coach, humanoidBones, blendTransition, breathing, resolveView,
         placeCamera, blendView } from "./v3/coach3d.js?v=20260824-ankles5";

const canvas = document.getElementById("yoga-3d-canvas");
const stage = document.getElementById("game-stage");
let active = stage.classList.contains("yoga-active");
let latest = null;
let data = null;
let coach = null;
let renderer = null;
let scene = null;
let camera = null;
let failed = false;

const api = {
  ready: false,
  setActive(value) { active = !!value; syncClass(); },
  applyState(game) { latest = game; },
  error: "",
};
window.Yoga3D = api;

function syncClass() {
  stage.classList.toggle("yoga-3d", active && api.ready && !failed);
}

function poseOf(id) {
  const authored = data.poses3d[id];
  if (authored) return authored;
  const scored = data.poses[id];
  if (scored) return { bones: scored.table, view: "standing",
                       camera: scored.camera, terminals: scored.terminals,
                       terminal3d: scored.terminal3d };
  return { bones: data.stand, view: "standing" };
}

function transitionPoses(fromId, toId) {
  return (data.transition_paths?.[`${fromId}>${toId}`] || []).map(poseOf);
}

function aim(from, to, t) {
  const ease = t <= 0 ? 0 : t >= 1 ? 1 : t * t * (3 - 2 * t);
  placeCamera(camera, blendView(resolveView(from, data.views),
                                resolveView(to, data.views), ease));
}

function frame(now) {
  requestAnimationFrame(frame);
  if (!active || !api.ready || !coach) return;
  const rig = latest?.rig || {};
  const toId = rig.pose || latest?.pose?.id || "mountain";
  const fromId = rig.from || rig.pose || "mountain";
  const to = poseOf(toId);
  const from = poseOf(fromId);
  const t = Number.isFinite(Number(rig.blend)) ? Number(rig.blend) : 1;
  const moving = latest?.state === "playing" && t < 0.999;
  const shown = moving
    ? blendTransition(from, to, transitionPoses(fromId, toId), t) : to;
  coach.apply(shown, moving ? null : breathing(now / 1000, 1));
  aim(from, to, t);
  renderer.render(scene, camera);
}

async function initialise() {
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: false });
    renderer.setPixelRatio(1);
    renderer.setSize(1280, 800, false);
    renderer.outputColorSpace = THREE.SRGBColorSpace;

    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x000000);
    scene.fog = null;
    camera = new THREE.PerspectiveCamera(30, 1280 / 800, 0.1, 60);

    scene.add(new THREE.HemisphereLight(0xdff0ff, 0x6d8a4a, 2.0));
    const sun = new THREE.DirectionalLight(0xfff4dc, 1.5);
    sun.position.set(3, 6, 4);
    scene.add(sun);

    data = await fetch("/assets/yoga/v3/rigdata.json", { cache: "no-store" }).then((response) => {
      if (!response.ok) throw new Error(`rigdata ${response.status}`);
      return response.json();
    });
    const gltf = await new GLTFLoader().loadAsync("/assets/yoga/v3/models/coach.vrm");
    scene.add(gltf.scene);
    coach = new Coach(gltf.scene, humanoidBones(gltf));
    const mountain = poseOf("mountain");
    coach.apply(mountain);
    aim(mountain, mountain, 1);
    api.ready = true;
    syncClass();
  } catch (error) {
    failed = true;
    api.error = String(error?.message || error);
    console.error("Yoga 3D coach unavailable; using 2D fallback", error);
    syncClass();
  }
}

requestAnimationFrame(frame);
initialise();
