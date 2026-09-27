#!/usr/bin/env python3
# Copyright 2026 Creative Stack Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Runs inside Blender. Never import this from the host side.

Reads a JSON job from $CREATIVE_STACK_JOB and does one of:

    build_studio   write templates/blender/studio.blend — camera, three-point
                   lighting, a shadow-catching floor
    render         import a model into the studio and render stills, a
                   turntable, or an isometric diagram view
    logo           extrude an SVG into 3D and render it, optionally spinning

Anything this prints with a `CS:` prefix is forwarded to the user by blendlib;
everything else is Blender's own noise.
"""

import json
import math
import os
import sys

import bpy
import mathutils

JOB = json.loads(os.environ.get("CREATIVE_STACK_JOB", "{}"))


def say(msg):
    print(f"CS: {msg}")
    sys.stdout.flush()


def srgb_to_linear(hex_colour, alpha=1.0):
    """Blender's colour sockets are linear; brand tokens are sRGB hex.

    Skipping this conversion is the single most common way a render comes back
    looking washed out — #4285F4 fed in raw lands somewhere around #7FB0FF.
    """
    value = hex_colour.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    out = []
    for i in (0, 2, 4):
        channel = int(value[i:i + 2], 16) / 255.0
        out.append(channel / 12.92 if channel <= 0.04045
                   else ((channel + 0.055) / 1.055) ** 2.4)
    return (*out, alpha)


# ------------------------------------------------------------------ scene ---

def clear_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_camera(scene, name="StudioCamera"):
    data = bpy.data.cameras.new(name)
    data.lens = 50
    obj = bpy.data.objects.new(name, data)
    scene.collection.objects.link(obj)
    scene.camera = obj
    return obj


def make_light(scene, name, kind, energy, size, location, rotation=(0, 0, 0)):
    data = bpy.data.lights.new(name, type=kind)
    data.energy = energy
    if kind == "AREA":
        data.size = size
        data.shape = "SQUARE"
    elif kind == "SUN":
        data.angle = math.radians(size)
    obj = bpy.data.objects.new(name, data)
    obj.location = location
    obj.rotation_euler = rotation
    scene.collection.objects.link(obj)
    return obj


def aim_at(obj, target=(0, 0, 0)):
    """Point an object's -Z axis at a world-space point."""
    direction = mathutils.Vector(target) - obj.location
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def build_three_point(scene):
    """Key, fill, rim — the arrangement every product shot is a variation of.

    Key is the bright one that defines the form, fill lifts the shadow side so
    it does not read as black, rim separates the subject from the background.
    Sizes are large relative to the subject because a big source gives soft
    shadows, which is what makes a render stop looking like a render.
    """
    key = make_light(scene, "Key", "AREA", 400, 4.0, (3.2, -3.6, 3.4))
    aim_at(key)
    fill = make_light(scene, "Fill", "AREA", 120, 5.0, (-3.8, -2.4, 1.6))
    aim_at(fill)
    rim = make_light(scene, "Rim", "AREA", 300, 2.5, (-1.4, 3.8, 2.8))
    aim_at(rim)
    # No backdrop lamp. The cove is never photographed — it only catches the
    # shadow, and the background behind it is composited. See `dress_floor`.
    return key, fill, rim


def build_floor(scene, radius=5.0, segments=12, sweep_start=3.0):
    """An infinity cove: floor that curves up into a wall with no seam.

    A flat plane puts a horizon line across every render — the floor is lit by
    the lamps, the world behind it is not, and the join between them reads as a
    grey band behind the subject. A cove is what a real product studio builds
    for the same reason: the eye has nothing to latch onto, so the subject
    floats on a continuous sweep.

    The profile is drawn in the YZ plane — flat toward the camera, a quarter
    arc, then straight up — and extruded along X. `sweep_start` is where the
    arc begins, and it has to sit well behind the subject: start it too close
    and the floor is already climbing underneath the model, which costs the
    contact shadow that makes the render look grounded.
    """
    profile = [(-14.0, 0.0)]
    profile += [(sweep_start + radius * math.sin(t * math.pi / 2 / segments),
                 radius - radius * math.cos(t * math.pi / 2 / segments))
                for t in range(segments + 1)]
    profile.append((sweep_start + radius, 14.0))

    half = 18.0
    verts, faces = [], []
    for y, z in profile:
        verts += [(-half, y, z), (half, y, z)]
    for i in range(len(profile) - 1):
        a = i * 2
        faces.append((a, a + 1, a + 3, a + 2))

    mesh = bpy.data.meshes.new("Cove")
    mesh.from_pydata(verts, [], faces)
    mesh.update()
    # Smooth shading across the arc: flat shading turns the curve into a dozen
    # visible facets, which is exactly the seam the cove exists to remove.
    for polygon in mesh.polygons:
        polygon.use_smooth = True

    floor = bpy.data.objects.new("Floor", mesh)
    scene.collection.objects.link(floor)
    return floor


def dress_floor(scene, engine="cycles"):
    """The cove catches shadows; it is never painted.

    Rendering the backdrop as lit geometry is the obvious approach and it does
    not work. The cove is metres across and the only lamps in the scene are
    aimed at a subject the size of a mug, so the floor by the camera blows past
    white while the wall behind sits around 75% grey — and the exposure that
    fixes the wall clips the contact shadow away. Adding an emission term to
    the sweep only makes it worse, because a 36x20m emitter is itself the
    brightest light in the scene.

    So the cove is a shadow catcher on transparent film, and the backdrop is a
    flat colour composited underneath. The background is then exactly the brand
    hex with no lighting in the loop at all, and the shadow is the only thing
    the cove contributes — which is the one thing it was for.
    """
    floor = bpy.data.objects.get("Floor")
    if not floor:
        return
    floor.is_shadow_catcher = True
    if engine == "eevee":
        # EEVEE ignores `is_shadow_catcher` and renders the cove as what it
        # is — an unlit grey plane across the whole frame. There is nothing to
        # salvage there, so the preview engine gets no contact shadow and a
        # clean backdrop instead of a correct-looking wrong one.
        floor.hide_render = True
        say("eevee: no contact shadow — it does not support shadow catchers. "
            "Use --engine cycles for the final.")


def set_world(scene, colour, strength=1.0):
    """The ambient fill.

    The film is transparent, so nothing here is ever photographed — this is a
    light, not a background. It still takes the backdrop colour, because the
    bounce landing on the subject should match whatever the subject will be
    composited onto.
    """
    world = bpy.data.worlds.new("Studio")
    scene.world = world
    world.use_nodes = True
    nodes, links = world.node_tree.nodes, world.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputWorld")
    background = nodes.new("ShaderNodeBackground")
    background.inputs["Strength"].default_value = strength
    background.inputs["Color"].default_value = srgb_to_linear(colour)
    links.new(background.outputs["Background"], output.inputs["Surface"])


def configure_render(scene, job):
    width = job.get("width", 1600)
    height = job.get("height", 1000)
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    # Always transparent. The cove is a shadow catcher, and Cycles only renders
    # one as transparent when the film is too — with an opaque film it falls
    # back to being an ordinary diffuse plane, which is the grey band across a
    # render that was meant to be white. The brand backdrop is flattened in
    # afterwards on the host, so `--transparent` just means "skip that step".
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"

    engine = job.get("engine", "cycles")
    if engine == "eevee":
        # The identifier changed with EEVEE Next; try the new one and fall
        # back rather than hard-failing on an older build.
        for name in ("BLENDER_EEVEE_NEXT", "BLENDER_EEVEE"):
            try:
                scene.render.engine = name
                break
            except TypeError:
                continue
        if hasattr(scene, "eevee"):
            scene.eevee.taa_render_samples = job.get("samples", 64)
    else:
        scene.render.engine = "CYCLES"
        scene.cycles.samples = job.get("samples", 96)
        scene.cycles.use_denoising = True
        # CPU by default: a headless box, a container and a CI runner all have
        # no usable GPU, and a silent fallback that takes 40x longer is worse
        # than being explicit.
        scene.cycles.device = "GPU" if job.get("gpu") else "CPU"

    # Blender 4.x defaults the view transform to AgX, which is a filmic tone
    # map — lovely for photography, wrong here. It desaturates and shifts every
    # brand colour, so a #4285F4 surface does not come back as #4285F4.
    scene.view_settings.view_transform = "Standard"
    scene.view_settings.look = "None"


# --------------------------------------------------------------- importers ---

IMPORTERS = {
    ".obj": [("wm", "obj_import"), ("import_scene", "obj")],
    ".gltf": [("import_scene", "gltf")],
    ".glb": [("import_scene", "gltf")],
    ".fbx": [("import_scene", "fbx")],
    ".stl": [("wm", "stl_import"), ("import_mesh", "stl")],
    ".ply": [("wm", "ply_import"), ("import_mesh", "ply")],
    ".dae": [("wm", "collada_import")],
    ".abc": [("wm", "alembic_import")],
    ".usd": [("wm", "usd_import")],
    ".usdc": [("wm", "usd_import")],
    ".usda": [("wm", "usd_import")],
    ".usdz": [("wm", "usd_import")],
    ".svg": [("import_curve", "svg")],
}


def import_model(path):
    """Import `path`, returning the objects it added.

    Operator names moved around in 4.x (`import_mesh.stl` became
    `wm.stl_import`, and so on), so each format lists its candidates newest
    first and we take whichever this build actually has.
    """
    suffix = os.path.splitext(path)[1].lower()
    before = set(bpy.data.objects)

    if suffix == ".blend":
        # Append every object from the file's top-level Object directory.
        with bpy.data.libraries.load(path, link=False) as (source, target):
            target.objects = list(source.objects)
        for obj in set(bpy.data.objects) - before:
            if obj.name not in bpy.context.scene.collection.objects:
                bpy.context.scene.collection.objects.link(obj)
    else:
        for module, operator in IMPORTERS.get(suffix, []):
            func = getattr(getattr(bpy.ops, module, None), operator, None)
            if func is None:
                continue
            if suffix == ".svg":
                enable_svg_importer()
            func(filepath=path)
            break
        else:
            raise RuntimeError(
                f"this Blender has no importer for {suffix}. Convert the model "
                f"to .glb, which every version since 2.8 reads natively.")

    added = [obj for obj in set(bpy.data.objects) - before]
    if not added:
        raise RuntimeError(f"{os.path.basename(path)} imported no objects — "
                           f"the file may be empty or corrupt")
    return added


def enable_svg_importer():
    if hasattr(bpy.ops.import_curve, "svg"):
        return
    for name in ("io_curve_svg", "bl_ext.blender_org.io_curve_svg"):
        try:
            bpy.ops.preferences.addon_enable(module=name)
            return
        except Exception:                                    # noqa: BLE001
            continue


# ------------------------------------------------------------------ framing ---

def bounds(objects):
    """World-space (min, max, centre, size) over every mesh/curve object."""
    points = []
    for obj in objects:
        if obj.type not in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
            continue
        for corner in obj.bound_box:
            points.append(obj.matrix_world @ mathutils.Vector(corner))
    if not points:
        raise RuntimeError("nothing renderable was imported — no mesh or curve "
                           "objects in the file")
    low = mathutils.Vector((min(p[i] for p in points) for i in range(3)))
    high = mathutils.Vector((max(p[i] for p in points) for i in range(3)))
    return low, high, (low + high) / 2, (high - low)


def normalise(objects, target_size=2.0):
    """Scale and centre the subject so one rig frames any model.

    Without this the camera distance would have to be tuned per file: CAD
    exports are often in millimetres, game assets in metres, and a scan in
    whatever the scanner felt like.
    """
    low, high, centre, size = bounds(objects)
    largest = max(size) or 1.0
    factor = target_size / largest

    pivot = bpy.data.objects.new("Subject", None)
    bpy.context.scene.collection.objects.link(pivot)
    for obj in objects:
        if obj.parent is None:
            obj.parent = pivot
    pivot.scale = (factor, factor, factor)
    # Sit the subject on the floor rather than through it: the shadow catcher
    # is at z=0 and a model centred on its origin is half buried.
    pivot.location = (-centre.x * factor, -centre.y * factor, -low.z * factor)
    bpy.context.view_layer.update()
    return pivot


def seat_on_floor(pivot, objects):
    """Re-centre `pivot` so its children sit on z=0, centred on the origin.

    `normalise` does this once, but anything that rotates the subject
    afterwards invalidates it.
    """
    bpy.context.view_layer.update()
    low, _, centre, _ = bounds(objects)
    pivot.location.x -= centre.x
    pivot.location.y -= centre.y
    pivot.location.z -= low.z
    bpy.context.view_layer.update()


def action_fcurves(obj):
    """Every F-curve on `obj`'s action, across both action layouts.

    Blender 4.4 replaced the flat `action.fcurves` list with slotted actions,
    where the curves live in a channelbag hanging off a strip on a layer, and
    5.x dropped the old attribute entirely. Both are still in the wild — Arch
    and Homebrew ship 5.x, Debian stable does not — so this asks rather than
    assumes.
    """
    animation = obj.animation_data
    action = animation.action if animation else None
    if action is None:
        return []
    if hasattr(action, "fcurves"):
        return list(action.fcurves)
    slot = getattr(animation, "action_slot", None)
    curves = []
    for layer in action.layers:
        for strip in layer.strips:
            bag = strip.channelbag(slot) if slot else None
            if bag:
                curves.extend(bag.fcurves)
    return curves


def place_camera(camera, view, distance, height_ratio=0.42, target_z=0.45):
    """Standard three-quarter, straight-on front, or true isometric."""
    if view == "iso":
        # Real isometric: 45 degrees around, and atan(1/sqrt(2)) above, which
        # is the angle that makes the three axes project to equal lengths.
        camera.data.type = "ORTHO"
        camera.data.ortho_scale = distance
        angle = math.radians(45)
        elevation = math.atan(1 / math.sqrt(2))
        camera.location = (distance * math.cos(angle) * math.cos(elevation),
                           -distance * math.sin(angle) * math.cos(elevation),
                           distance * math.sin(elevation))
    elif view == "front":
        camera.data.type = "PERSP"
        camera.location = (0, -distance, distance * 0.18)
    else:                                                     # three-quarter
        camera.data.type = "PERSP"
        camera.location = (distance * 0.62, -distance * 0.72,
                           distance * height_ratio)
    aim_at(camera, (0, 0, target_z))


# ------------------------------------------------------------------ actions ---

def action_build_studio(job):
    clear_scene()
    scene = bpy.context.scene
    build_three_point(scene)
    build_floor(scene)
    camera = make_camera(scene)
    place_camera(camera, "three-quarter", 6.0)
    set_world(scene, "#FFFFFF", strength=0.5)
    configure_render(scene, job)
    bpy.ops.wm.save_as_mainfile(filepath=job["output"])
    say(f"studio rig written to {job['output']}")


def action_render(job):
    bpy.ops.wm.open_mainfile(filepath=job["studio"])
    scene = bpy.context.scene
    configure_render(scene, job)

    palette = job.get("palette", {})
    # The world is never seen now that the film is transparent — it is purely
    # a fill light. It still takes the backdrop colour so the bounce matches
    # whatever the subject ends up sitting on.
    set_world(scene, palette.get("bg", "#FFFFFF"),
              strength=job.get("world_strength", 0.5))

    objects = import_model(job["model"])
    pivot = normalise(objects, job.get("size", 2.0))
    say(f"imported {len(objects)} object(s) from "
        f"{os.path.basename(job['model'])}")

    if job.get("material"):
        apply_material(objects, job["material"], palette)

    dress_floor(scene, job.get("engine", "cycles"))
    _, high, centre, _ = bounds([pivot] + list(objects))
    place_camera(scene.camera, job.get("view", "three-quarter"),
                 job.get("distance", 6.0), target_z=max(centre.z, high.z * 0.5))

    frames = job.get("frames", 0)
    if frames:
        # Spin the pivot, not the camera: rotating the camera drags the
        # lighting relative to the subject and the turntable strobes.
        pivot.rotation_euler = (0, 0, 0)
        pivot.keyframe_insert("rotation_euler", frame=1)
        pivot.rotation_euler = (0, 0, math.radians(360))
        pivot.keyframe_insert("rotation_euler", frame=frames + 1)
        for curve in action_fcurves(pivot):
            for point in curve.keyframe_points:
                point.interpolation = "LINEAR"
        # Frame `frames + 1` is the same pose as frame 1, so stopping one short
        # is what makes the loop seamless.
        scene.frame_start, scene.frame_end = 1, frames
        scene.render.filepath = job["output"]
        bpy.ops.render.render(animation=True)
        say(f"rendered {frames} frames to {job['output']}####.png")
    else:
        scene.render.filepath = job["output"]
        bpy.ops.render.render(write_still=True)
        say(f"rendered {job['output']}")


def apply_material(objects, spec, palette):
    """Override every material with one brand-coloured surface.

    Imported models arrive with whatever the artist left on them — often a
    magenta missing-texture placeholder. For a diagram or a product shot you
    almost always want one clean material instead.
    """
    colour = palette.get(spec, spec if spec.startswith("#") else "#4285F4")
    material = bpy.data.materials.new("Brand")
    material.use_nodes = True
    bsdf = material.node_tree.nodes.get("Principled BSDF")
    if bsdf:
        bsdf.inputs["Base Color"].default_value = srgb_to_linear(colour)
        bsdf.inputs["Roughness"].default_value = 0.35
        if "Metallic" in bsdf.inputs:
            bsdf.inputs["Metallic"].default_value = 0.1
    for obj in objects:
        if obj.type not in {"MESH", "CURVE", "SURFACE", "FONT"}:
            continue
        obj.data.materials.clear()
        obj.data.materials.append(material)


def action_logo(job):
    """Extrude an SVG into 3D and render it, still or spinning."""
    bpy.ops.wm.open_mainfile(filepath=job["studio"])
    scene = bpy.context.scene
    configure_render(scene, job)

    palette = job.get("palette", {})
    # The world is never seen now that the film is transparent — it is purely
    # a fill light. It still takes the backdrop colour so the bounce matches
    # whatever the subject ends up sitting on.
    set_world(scene, palette.get("bg", "#FFFFFF"),
              strength=job.get("world_strength", 0.5))

    curves = [obj for obj in import_model(job["svg"]) if obj.type == "CURVE"]
    if not curves:
        raise RuntimeError("that SVG imported no curves. Run it through "
                           "/vector `svg.py --op text-to-path` first: the SVG "
                           "importer reads paths, not live <text>.")

    # Extrusion is a fraction of the logo's own size, not an absolute distance.
    # Blender's SVG importer maps 1px to 1/1000 of a unit, so a 24px icon and a
    # 2400px wordmark arrive three orders of magnitude apart and a fixed depth
    # would be invisible on one and a solid block on the other. `extrude`
    # pushes both ways from the curve, so it is half the thickness asked for.
    _, _, _, flat = bounds(curves)
    span = max(flat) or 1.0
    for curve in curves:
        curve.data.extrude = job.get("depth", 0.08) * span / 2
        curve.data.bevel_depth = job.get("bevel", 0.004) * span
        curve.data.bevel_resolution = 3
        # Logos are drawn as overlapping subpaths constantly; without this the
        # extrusion self-intersects into a mess of shading artefacts.
        curve.data.fill_mode = "BOTH"
        curve.data.use_fill_caps = True

    pivot = normalise(curves, job.get("size", 2.0))
    # SVG imports lie flat in the XY plane facing +Z; stand it up to face the
    # camera. That happens after `normalise` has already seated the flat
    # version on the floor, so the standing logo has to be re-seated — half of
    # it is otherwise below z=0 and buried in the cove.
    pivot.rotation_euler = (math.radians(90), 0, 0)
    seat_on_floor(pivot, curves)

    apply_material(curves, job.get("material", "accent"), palette)
    dress_floor(scene, job.get("engine", "cycles"))
    _, high, centre, _ = bounds([pivot] + list(curves))
    place_camera(scene.camera, "front", job.get("distance", 5.0),
                 target_z=max(centre.z, high.z * 0.5))

    frames = job.get("frames", 0)
    if frames:
        spin = bpy.data.objects.new("Spin", None)
        scene.collection.objects.link(spin)
        pivot.parent = spin
        spin.rotation_euler = (0, 0, math.radians(-30))
        spin.keyframe_insert("rotation_euler", frame=1)
        spin.rotation_euler = (0, 0, math.radians(30))
        spin.keyframe_insert("rotation_euler", frame=frames)
        for curve in action_fcurves(spin):
            for point in curve.keyframe_points:
                # Bezier, not linear: a sting that eases in and out reads as
                # deliberate; constant angular velocity reads as a screensaver.
                point.interpolation = "BEZIER"
                point.handle_left_type = point.handle_right_type = "AUTO_CLAMPED"
        scene.frame_start, scene.frame_end = 1, frames
        scene.render.filepath = job["output"]
        bpy.ops.render.render(animation=True)
        say(f"rendered {frames} frames to {job['output']}####.png")
    else:
        scene.render.filepath = job["output"]
        bpy.ops.render.render(write_still=True)
        say(f"rendered {job['output']}")


ACTIONS = {"build_studio": action_build_studio, "render": action_render,
           "logo": action_logo}


def main():
    action = JOB.get("action")
    handler = ACTIONS.get(action)
    if not handler:
        print(f"CS: unknown action {action!r}", file=sys.stderr)
        sys.exit(1)
    handler(JOB)


if __name__ == "__main__":
    main()
