"""Generate the Nightjar cabinet drawings.

Isometric projection: x runs to the lower right, y to the lower left, z up.
Viewer sits toward +x/+y/+z, so the visible outer face is x=W, and with the
top and front panel removed we see the floor plus the inner faces at y=0
and x=0.
"""

import math

# ---- cabinet internal dimensions, mm -------------------------------------
W, D, H = 400, 250, 120          # width, depth, height
DEV_W, DEV_D, DEV_H = 179, 183, 34.5   # ThinkCentre M720q lying flat
REAR_PLENUM = 40                  # gap behind the devices for exhaust
WALL = 1.5                        # sheet thickness

INK = "#0F1420"
AMBER = "#B4741F"
BONE = "#F7F5EF"

COS30 = math.cos(math.radians(30))


def iso(x, y, z, s=0.80, ox=285.0, oy=140.0):
    return (ox + (x - y) * COS30 * s, oy + (x + y) * 0.5 * s - z * s)


def poly(points, fill, stroke=INK, sw=1.2, opacity=1.0, dash=None):
    pts = " ".join(f"{px:.1f},{py:.1f}" for px, py in points)
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" '
        f'stroke-width="{sw}" opacity="{opacity}"{d}/>'
    )


def iso_box(x0, y0, z0, dx, dy, dz, top, right, front):
    """Three visible faces of a box: top, the x=x0+dx face, the y=y0+dy face."""
    x1, y1, z1 = x0 + dx, y0 + dy, z0 + dz
    out = []
    # top
    out.append(poly([iso(x0, y0, z1), iso(x1, y0, z1), iso(x1, y1, z1), iso(x0, y1, z1)], top))
    # right (x = x1)
    out.append(poly([iso(x1, y0, z0), iso(x1, y1, z0), iso(x1, y1, z1), iso(x1, y0, z1)], right))
    # front (y = y1)
    out.append(poly([iso(x0, y1, z0), iso(x1, y1, z0), iso(x1, y1, z1), iso(x0, y1, z1)], front))
    return out


def iso_ellipse(cx, cy, r, fill, stroke=AMBER, sw=1.3, dash=None):
    """A circle lying in the z-plane, projected: becomes an ellipse."""
    px, py = iso(cx, cy, 0)
    rx = r * COS30 * 0.80 * 1.414
    ry = r * 0.5 * 0.80 * 1.414
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<ellipse cx="{px:.1f}" cy="{py:.1f}" rx="{rx:.1f}" ry="{ry:.1f}" '
        f'fill="{fill}" stroke="{stroke}" stroke-width="{sw}" '
        f'transform="rotate(-30 {px:.1f} {py:.1f})"{d}/>'
    )


def arrow(x1, y1, x2, y2, colour=AMBER, sw=1.6, dash=None):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (
        f'<path d="M{x1:.1f} {y1:.1f} L{x2:.1f} {y2:.1f}" fill="none" '
        f'stroke="{colour}" stroke-width="{sw}" marker-end="url(#ar)"{d}/>'
    )


def txt(x, y, s, cls="n", anchor="start", rot=None):
    t = f' transform="rotate({rot} {x} {y})"' if rot else ""
    return f'<text class="{cls}" x="{x}" y="{y}" text-anchor="{anchor}"{t}>{s}</text>'


HEAD = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="{alt}">
<title>{title}</title>
<desc>{desc}</desc>
<defs>
<marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6.5" markerHeight="6.5" orient="auto-start-reverse">
<path d="M2 2 L9 5 L2 8" fill="none" stroke="context-stroke" stroke-width="1.4" stroke-linecap="round"/>
</marker>
<marker id="dm" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
<path d="M3 2 L7 5 L3 8" fill="none" stroke="context-stroke" stroke-width="1.3" stroke-linecap="round"/>
</marker>
<pattern id="filt" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(30)">
<line x1="0" y1="0" x2="0" y2="6" stroke="#B4741F" stroke-width="1" opacity="0.5"/>
</pattern>
</defs>
<style>
.t {{ font: 600 14px ui-sans-serif,-apple-system,"Segoe UI",sans-serif; fill:#0F1420; letter-spacing:1.2px; }}
.st {{ font: 400 11px ui-sans-serif,-apple-system,"Segoe UI",sans-serif; fill:#0F1420; opacity:.65; }}
.n {{ font: 400 11.5px ui-sans-serif,-apple-system,"Segoe UI",sans-serif; fill:#0F1420; }}
.nh {{ font: 600 11.5px ui-sans-serif,-apple-system,"Segoe UI",sans-serif; fill:#0F1420; }}
.dim {{ font: 500 11px ui-monospace,"SF Mono",Menlo,monospace; fill:#B4741F; }}
.lb {{ font: 500 10.5px ui-sans-serif,-apple-system,"Segoe UI",sans-serif; fill:#0F1420; }}
</style>
<rect width="{w}" height="{h}" fill="#F7F5EF"/>
"""


# =========================================================================
# DRAWING 1 — isometric cutaway
# =========================================================================
def isometric() -> str:
    o = [
        HEAD.format(
            w=920, h=560,
            alt="Isometric cutaway of the Nightjar cabinet with top and front panel removed",
            title="Nightjar cabinet - isometric cutaway",
            desc=(
                "Isometric view with the lid and front panel removed. Two ThinkCentre "
                "M720q units lie flat side by side with their front grilles facing the "
                "cabinet front and a 40 mm rear plenum behind them. Two filtered 80 mm "
                "intake fans sit in the floor, two 80 mm exhaust fans in the lid above "
                "the rear plenum, and a panel mount HDMI coupler exits the lid."
            ),
        ),
        txt(40, 34, "CABINET — ISOMETRIC CUTAWAY", "t"),
        txt(40, 52, "lid and front panel removed · internal 400 × 250 × 120 · all dims mm", "st"),
    ]

    # --- shell: far inner faces first -----------------------------------
    # rear inner wall (y = 0)
    o.append(poly([iso(0, 0, 0), iso(W, 0, 0), iso(W, 0, H), iso(0, 0, H)], "#EAE6DA"))
    # left inner wall (x = 0)
    o.append(poly([iso(0, 0, 0), iso(0, D, 0), iso(0, D, H), iso(0, 0, H)], "#E2DED1"))
    # floor
    o.append(poly([iso(0, 0, 0), iso(W, 0, 0), iso(W, D, 0), iso(0, D, 0)], "#F2EFE6"))
    # right outer wall (x = W), seen from outside
    o.append(poly([iso(W, 0, 0), iso(W, D, 0), iso(W, D, H), iso(W, 0, H)], "#D8D3C4"))

    # --- intake fans in the floor, under each device --------------------
    for cx in (100, 300):
        o.append(iso_ellipse(cx, 200, 40, "url(#filt)"))
        o.append(iso_ellipse(cx, 200, 12, "none", dash="3 3"))

    # --- the two devices ------------------------------------------------
    dev_y = D - REAR_PLENUM - DEV_D          # 250-40-183 = 27
    for x0 in (12, 209):
        o += iso_box(x0, dev_y, 8, DEV_W, DEV_D, DEV_H,
                     top="#FFFFFF", right="#E9E5D9", front="#DDD8C9")

    px, py = iso(101, dev_y + DEV_D / 2, 8 + DEV_H)
    o.append(txt(px, py, "M720q", "lb", "middle"))
    px, py = iso(298, dev_y + DEV_D / 2, 8 + DEV_H)
    o.append(txt(px, py, "bay 2", "lb", "middle"))

    # --- rear plenum callout --------------------------------------------
    p1 = iso(200, dev_y + DEV_D, 0)
    p2 = iso(200, D, 0)
    o.append(
        f'<path d="M{p1[0]:.1f} {p1[1]:.1f} L{p2[0]:.1f} {p2[1]:.1f}" fill="none" '
        f'stroke="{AMBER}" stroke-width="1.3" marker-start="url(#dm)" marker-end="url(#dm)"/>'
    )

    # --- exhaust fans in the lid plane, above the rear plenum -----------
    for cx in (100, 300):
        px, py = iso(cx, 20, H)
        rx, ry = 40 * COS30 * 0.8 * 1.414, 40 * 0.5 * 0.8 * 1.414
        o.append(
            f'<ellipse cx="{px:.1f}" cy="{py:.1f}" rx="{rx:.1f}" ry="{ry:.1f}" '
            f'fill="none" stroke="{AMBER}" stroke-width="1.3" stroke-dasharray="5 4" '
            f'transform="rotate(-30 {px:.1f} {py:.1f})"/>'
        )
        o.append(arrow(px, py - 6, px, py - 52))

    # --- top opening rim ------------------------------------------------
    o.append(poly([iso(0, 0, H), iso(W, 0, H), iso(W, D, H), iso(0, D, H)],
                  "none", INK, 1.5))

    # --- ghosted front panel position -----------------------------------
    o.append(poly([iso(0, D, 0), iso(W, D, 0), iso(W, D, H), iso(0, D, H)],
                  "none", INK, 1.1, 0.5, "6 5"))

    # --- HDMI coupler in the lid ----------------------------------------
    px, py = iso(30, 15, H)
    o.append(f'<rect x="{px - 13:.1f}" y="{py - 7:.1f}" width="26" height="14" rx="2" '
             f'fill="#FFFFFF" stroke="{INK}" stroke-width="1.3"/>')
    o.append(arrow(px, py - 10, px, py - 46, INK, 1.4))
    o.append(txt(px + 6, py - 52, "HDMI up to screen", "lb"))

    # --- intake arrows from below ---------------------------------------
    for cx in (100, 300):
        px, py = iso(cx, 200, 0)
        o.append(arrow(px, py + 46, px, py + 6))

    # --- notes ----------------------------------------------------------
    nx = 640
    rows = [
        ("nh", "LAYOUT"),
        ("n", "Devices flat, side by side, front"),
        ("n", "grilles toward the front panel."),
        ("n", "40 mm rear plenum — their own"),
        ("n", "exhaust vents into it."),
        ("", ""),
        ("nh", "AIRFLOW"),
        ("n", "2 × 80 mm filtered intake in the"),
        ("n", "floor, one under each bay."),
        ("n", "2 × 80 mm exhaust in the lid,"),
        ("n", "directly over the rear plenum."),
        ("n", "Bottom-to-top: convection helps"),
        ("n", "rather than fights the fans."),
        ("n", "Run intake slightly stronger —"),
        ("n", "positive pressure keeps dust out."),
        ("", ""),
        ("nh", "PANELS"),
        ("n", "Lid and front panel both bolted,"),
        ("n", "not welded. You will revise them."),
        ("n", "Filters serviceable from outside"),
        ("n", "without opening the cabinet."),
        ("", ""),
        ("nh", "DO NOT"),
        ("n", "Mount HDMI on the front — the"),
        ("n", "screen is above; a front exit"),
        ("n", "means a visible cable loop."),
    ]
    y = 100
    for cls, s in rows:
        if s:
            o.append(txt(nx, y, s, cls))
        y += 17

    o.append("</svg>")
    return "\n".join(o)


# =========================================================================
# DRAWING 2 — plan, side section, rear elevation
# =========================================================================
def views() -> str:
    S = 0.82   # px per mm

    def P(x, y, ox, oy):
        return ox + x * S, oy + y * S

    o = [
        HEAD.format(
            w=940, h=760,
            alt="Plan, side section and rear elevation of the Nightjar cabinet",
            title="Nightjar cabinet - plan, side section, rear elevation",
            desc=(
                "Three orthographic views. Plan with the lid removed shows two devices "
                "side by side, the rear plenum, floor intake fans and lid exhaust "
                "positions. Side section shows the bottom-to-top airflow path and the "
                "HDMI route. Rear elevation shows the sealed rear face against the wall."
            ),
        ),
    ]

    # ---------------- PLAN ----------------
    ox, oy = 60, 90
    o.append(txt(60, 40, "PLAN — LID REMOVED", "t"))
    o.append(txt(60, 58, "looking down · 400 × 250", "st"))

    x0, y0 = P(0, 0, ox, oy)
    o.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{W * S:.1f}" height="{D * S:.1f}" '
             f'rx="3" fill="#FFFFFF" stroke="{INK}" stroke-width="1.6"/>')

    dev_y = D - REAR_PLENUM - DEV_D
    for dx, label in ((12, "M720q"), (209, "bay 2")):
        px, py = P(dx, dev_y, ox, oy)
        o.append(f'<rect x="{px:.1f}" y="{py:.1f}" width="{DEV_W * S:.1f}" '
                 f'height="{DEV_D * S:.1f}" rx="2" fill="#F2EFE6" stroke="{INK}" '
                 f'stroke-width="1.3"/>')
        cx, cy = P(dx + DEV_W / 2, dev_y + DEV_D / 2, ox, oy)
        o.append(txt(cx, cy, label, "lb", "middle"))
        # the device's own rear exhaust vent
        vx, vy = P(dx + 20, dev_y, ox, oy)
        o.append(f'<line x1="{vx:.1f}" y1="{vy:.1f}" x2="{vx + 100 * S:.1f}" '
                 f'y2="{vy:.1f}" stroke="{AMBER}" stroke-width="2.4"/>')

    # rear plenum band
    px, py = P(0, 0, ox, oy)
    o.append(f'<rect x="{px:.1f}" y="{py:.1f}" width="{W * S:.1f}" '
             f'height="{REAR_PLENUM * S:.1f}" fill="{AMBER}" opacity="0.13"/>')
    cx, cy = P(W / 2, REAR_PLENUM / 2, ox, oy)
    o.append(txt(cx, cy + 4, "rear plenum 40", "dim", "middle"))

    # exhaust fans in lid (dashed, above)
    for fx in (100, 300):
        cx, cy = P(fx, 20, ox, oy)
        o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{40 * S:.1f}" fill="none" '
                 f'stroke="{AMBER}" stroke-width="1.3" stroke-dasharray="5 4"/>')
    # intake fans in floor
    for fx in (100, 300):
        cx, cy = P(fx, 200, ox, oy)
        o.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{40 * S:.1f}" '
                 f'fill="url(#filt)" stroke="{AMBER}" stroke-width="1.3"/>')
    cx, cy = P(200, 200, ox, oy)
    o.append(txt(cx, cy + 4, "intake (floor)", "dim", "middle"))
    cx, cy = P(200, 20, ox, oy)
    o.append(txt(cx, cy + 4, "exhaust (lid)", "dim", "middle"))

    # HDMI coupler in lid
    cx, cy = P(30, 15, ox, oy)
    o.append(f'<rect x="{cx - 12:.1f}" y="{cy - 7:.1f}" width="24" height="14" rx="2" '
             f'fill="#FFF" stroke="{INK}" stroke-width="1.2" stroke-dasharray="4 3"/>')
    o.append(txt(cx - 16, cy + 26, "HDMI", "dim"))

    # dims
    ax, ay = P(0, D, ox, oy)
    bx, _ = P(W, D, ox, oy)
    o.append(f'<path d="M{ax:.1f} {ay + 22:.1f} L{bx:.1f} {ay + 22:.1f}" fill="none" '
             f'stroke="{AMBER}" stroke-width="1.2" marker-start="url(#dm)" marker-end="url(#dm)"/>')
    o.append(txt((ax + bx) / 2, ay + 18, "400", "dim", "middle"))
    o.append(f'<path d="M{ax - 22:.1f} {oy:.1f} L{ax - 22:.1f} {ay:.1f}" fill="none" '
             f'stroke="{AMBER}" stroke-width="1.2" marker-start="url(#dm)" marker-end="url(#dm)"/>')
    o.append(txt(ax - 26, (oy + ay) / 2, "250", "dim", "end"))

    # ---------------- SIDE SECTION ----------------
    ox2, oy2 = 620, 90
    o.append(txt(620, 40, "SIDE SECTION", "t"))
    o.append(txt(620, 58, "airflow · 250 deep × 120 high", "st"))

    sx, sy = ox2, oy2
    o.append(f'<rect x="{sx:.1f}" y="{sy:.1f}" width="{D * S:.1f}" height="{H * S:.1f}" '
             f'rx="3" fill="#FFFFFF" stroke="{INK}" stroke-width="1.6"/>')
    # device in section: depth 183, height 34.5, sitting 8 above floor
    dpx = sx + (D - REAR_PLENUM - DEV_D) * S
    dpy = sy + (H - 8 - DEV_H) * S
    o.append(f'<rect x="{dpx:.1f}" y="{dpy:.1f}" width="{DEV_D * S:.1f}" '
             f'height="{DEV_H * S:.1f}" rx="2" fill="#F2EFE6" stroke="{INK}" stroke-width="1.3"/>')
    o.append(txt(dpx + DEV_D * S / 2, dpy + DEV_H * S / 2 + 4, "M720q 34.5", "lb", "middle"))

    # airflow: up through floor at front, across, out the top rear
    fx = sx + 200 * S
    o.append(arrow(fx, sy + H * S + 34, fx, sy + H * S - 6))
    o.append(arrow(fx, sy + H * S - 12, fx - 130, sy + 34))
    ex = sx + 20 * S
    o.append(arrow(ex, sy + 22, ex, sy - 30))
    o.append(txt(ex + 8, sy - 34, "exhaust", "dim"))
    o.append(txt(fx - 6, sy + H * S + 50, "filtered intake", "dim", "middle"))

    # rear face against wall
    o.append(f'<rect x="{sx - 16:.1f}" y="{sy - 40:.1f}" width="12" '
             f'height="{H * S + 80:.1f}" fill="url(#filt)" stroke="{INK}" stroke-width="1.2"/>')
    o.append(txt(sx - 22, sy + H * S + 56, "wall", "st", "end"))

    # HDMI route
    o.append(f'<path d="M{dpx + 6:.1f} {dpy + 6:.1f} L{ex + 6:.1f} {dpy + 6:.1f} '
             f'L{ex + 6:.1f} {sy + 18:.1f}" fill="none" stroke="{INK}" '
             f'stroke-width="1.3" stroke-dasharray="4 3"/>')
    o.append(txt(dpx + 12, dpy - 6, "HDMI to lid coupler", "lb"))

    # ---------------- REAR ELEVATION ----------------
    ox3, oy3 = 620, 330
    o.append(txt(620, 300, "REAR ELEVATION", "t"))
    o.append(txt(620, 318, "sealed — faces the wall", "st"))
    rx, ry = ox3, oy3
    o.append(f'<rect x="{rx:.1f}" y="{ry:.1f}" width="{W * S * 0.72:.1f}" '
             f'height="{H * S:.1f}" rx="3" fill="#EAE6DA" stroke="{INK}" stroke-width="1.6"/>')
    for i in range(4):
        mx = rx + 24 + i * 68
        o.append(f'<rect x="{mx:.1f}" y="{ry + 12:.1f}" width="10" height="6" rx="2" '
                 f'fill="none" stroke="{AMBER}" stroke-width="1.2"/>')
        o.append(f'<rect x="{mx:.1f}" y="{ry + H * S - 18:.1f}" width="10" height="6" rx="2" '
                 f'fill="none" stroke="{AMBER}" stroke-width="1.2"/>')
    o.append(txt(rx + 8, ry + H * S + 24, "slotted mounting ears ×8 — level after anchoring", "n"))
    o.append(txt(rx + 8, ry + H * S + 42, "no vents on this face: it is 40 mm from plaster", "n"))
    o.append(txt(rx + 8, ry + H * S + 60, "cable exit grommet on the underside, not here", "n"))

    # ---------------- NOTES ----------------
    o.append(txt(60, 430, "WHY BOTTOM-TO-TOP", "nh"))
    notes = [
        "The devices exhaust rearward into the plenum. Lid fans sit directly",
        "above it, so hot air leaves by the shortest path and never crosses",
        "an intake. Convection pushes the same direction as the fans, so the",
        "cabinet still behaves sanely if a fan fails.",
        "",
        "Intake in the floor rather than the front frees the whole front panel",
        "for the OLED, touch pad and ports — and puts the filters where you",
        "can pull them without opening the cabinet.",
        "",
        "Acceptance target unchanged: intake air within 5 °C of room ambient,",
        "package under 80 °C sustained, NVMe under 65 °C, zero throttle events.",
    ]
    y = 452
    for line in notes:
        if line:
            o.append(txt(60, y, line, "n"))
        y += 17

    o.append("</svg>")
    return "\n".join(o)


if __name__ == "__main__":
    with open("assets/case-isometric.svg", "w") as fh:
        fh.write(isometric())
    with open("assets/case-views.svg", "w") as fh:
        fh.write(views())
    print("wrote assets/case-isometric.svg and assets/case-views.svg")
